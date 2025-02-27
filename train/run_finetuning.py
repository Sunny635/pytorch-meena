import warnings

warnings.filterwarnings("ignore")
import sys

sys.path.append('../')

import torch

from torch.utils.data import DataLoader, random_split

from tqdm import tqdm
from transformers import BertTokenizer
from fairseq.optim.adafactor import Adafactor
from apex import amp

import os
import json
import logging
from datetime import datetime
from model.meena import Meena
from common.arg import ModelConfig
from common.dataset import DatasetForSeq2seqV2, DatasetForSeq2seqConversation

class MeenaTrainer(object):
  def __init__(self,
               dataset,
               model,
               tokenizer,
               max_len,
               model_name,
               checkpoint_path,
               device=None,
               train_batch_size=8,
               eval_batch_size=None,
               log_dir='../logs',
               fp16=True):

    self.dataset = dataset
    self.model = model
    self.tokenizer = tokenizer
    self.max_len = max_len
    self.model_name = model_name
    self.checkpoint_path = checkpoint_path
    self.device = device
    self.n_gpu = torch.cuda.device_count() if torch.cuda.is_available() else 0
    self.train_batch_size = train_batch_size
    self.eval_batch_size = eval_batch_size
    self.log_dir = log_dir
    self.fp16 = fp16

    if device is None:
      self.device = 'cuda:1' if torch.cuda.is_available() else 'cpu'

    if eval_batch_size is None:
      self.eval_batch_size = train_batch_size

    logging.basicConfig(filename=f'{log_dir}/{self.model_name}-{datetime.now().date()}.log', level=logging.INFO)

  def build_dataloaders(self, train_test_split=0.1, train_shuffle=True, eval_shuffle=True):
    dataset_len = len(self.dataset)
    eval_len = int(dataset_len * train_test_split)
    train_len = dataset_len - eval_len
    train_dataset, eval_dataset = random_split(self.dataset, (train_len, eval_len))
    train_loader = DataLoader(train_dataset, batch_size=self.train_batch_size, shuffle=train_shuffle)
    eval_loader = DataLoader(eval_dataset, batch_size=self.eval_batch_size, shuffle=eval_shuffle)
    logging.info(f'''train_dataloader size: {len(train_loader.dataset)} | shuffle: {train_shuffle}
                         eval_dataloader size: {len(eval_loader.dataset)} | shuffle: {eval_shuffle}''')

    return train_loader, eval_loader

  def train(self,
            epochs,
            train_dataloader,
            eval_dataloader,
            optimizer,
            log_steps,
            ckpt_steps,
            gradient_accumulation_steps=1):
    losses = {}
    global_steps = 0
    local_steps = 0
    step_loss = 0.0
    start_epoch = 0
    start_step = 0
    step_perplexity = 0.0

    # Logging
    logging.info(f'{datetime.now()} | Moved model to: {self.device}')
    logging.info(
      f'{datetime.now()} | train_batch_size: {self.train_batch_size} | eval_batch_size: {self.eval_batch_size}')
    logging.info(f'{datetime.now()} | Epochs: {epochs} | log_steps: {log_steps} | ckpt_steps: {ckpt_steps}')
    logging.info(f'{datetime.now()} | gradient_accumulation_steps: {gradient_accumulation_steps}')

    # Train
    self.model.zero_grad()  # Reset gradients tensors
    for epoch in range(start_epoch, epochs):  # tqdm(range(epochs), desc='Epochs', position=0):
      logging.info(f'{datetime.now()} | Epoch: {epoch}')
      pb = tqdm(enumerate(train_dataloader),
                desc=f'Epoch-{epoch} Iterator',
                total=len(train_dataloader),
                bar_format='{l_bar}{bar:10}{r_bar}'
                )
      for step, batch in pb:
        # if step < start_step:
          # continue
        encoder_input_ids, decoder_input_ids, encoder_input_mask, labels = batch  # _ is input_mask
        encoder_input_ids, decoder_input_ids, encoder_input_mask, labels = encoder_input_ids.to(self.device), decoder_input_ids.to(self.device), encoder_input_mask.to(self.device), labels.to(self.device)
        output = self.model(encoder_input_ids, decoder_input_ids, encoder_input_mask, labels) # output: lm_logits, loss, encoder_logit, x

        loss = output[1]

        step_perplexity += torch.exp(loss)
        origin_loss = loss.item()

        loss = loss / gradient_accumulation_steps  # divide loss into gradient accumulation step
        if self.fp16:
          with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()
        else:
          loss.backward()

        step_loss += origin_loss
        losses[global_steps] = origin_loss

        local_steps += 1
        global_steps += 1

        if global_steps % gradient_accumulation_steps == 0:
          if self.fp16:
            torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), max_norm=1.0)
          else:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)

          optimizer.step()
          self.model.zero_grad()

        if global_steps % log_steps == 0:
          pb.set_postfix_str(
            f''' Train Loss: {format(step_loss / local_steps, ".4f")} | step_perplexity: {format(step_perplexity/local_steps,".4f")} | Steps: {global_steps}''')
          step_loss = 0.0
          local_steps = 0
          step_perplexity =0.0

        if global_steps % ckpt_steps == 0:
          self.save(epoch, self.model, optimizer, losses, global_steps)
          logging.info(f'{datetime.now()} | Saved checkpoint to: {self.checkpoint_path}')
          with open(f'{self.log_dir}/{self.model_name}_train_results.json', 'w') as results_file:
            json.dump(losses, results_file)
            results_file.close()

      # Evaluate every epoch
      self.evaluate(eval_dataloader)
      self.model.train()
      start_step = 0

    self.save(epoch, self.model, optimizer, losses, global_steps)

    return self.model

  def evaluate(self, dataloader):
    self.model.eval()

    eval_loss = 0.0
    perplexity = 0.0
    eval_steps = 0

    logging.info(f'{datetime.now()} | Evaluating {self.model_name}')
    for step, batch in tqdm(enumerate(dataloader),
                            desc='Evaluating',
                            leave=True,
                            total=len(dataloader),
                            bar_format='{l_bar}{bar:10}{r_bar}'):

      encoder_input_ids, decoder_input_ids, encoder_input_mask, labels = batch  # _ is input_mask
      encoder_input_ids, decoder_input_ids, encoder_input_mask, labels = encoder_input_ids.to(self.device), decoder_input_ids.to(self.device), encoder_input_mask.to(self.device), labels.to(self.device)

      with torch.no_grad():
        output = self.model(encoder_input_ids, decoder_input_ids, encoder_input_mask, labels) # output: lm_logits, loss, encoder_logit, x

      tmp_eval_loss = output[1]
      tmp_perplexity = torch.exp(tmp_eval_loss)

      if self.n_gpu > 1:
        tmp_eval_loss = tmp_eval_loss.mean()

      eval_loss += tmp_eval_loss.item()
      perplexity += tmp_perplexity.item()
      eval_steps += 1

      total_eval_loss = eval_loss / eval_steps
      total_perplexity = perplexity / eval_steps

      logging.info(f'{datetime.now()} | Step: {step} | Eval Loss: {total_eval_loss} | Perplexity: {total_perplexity}')
      with open(f'{self.log_dir}/{self.model_name}_eval_results.txt', 'a+') as results_file:
        results_file.write(f'{datetime.now()} | Step: {step} | Eval Loss: {total_eval_loss} | Perplexity: {total_perplexity}\n')
        results_file.close()

  def save(self, epoch, model, optimizer, losses, train_step):
    model.cpu()
    torch.save({
      'epoch': epoch,  # 현재 학습 epoch
      'model_state_dict': model.state_dict(),  # 모델 저장
      'optimizer_state_dict': optimizer.state_dict(),  # 옵티마이저 저장
      'losses': losses,  # Loss 저장
      'train_step': train_step,  # 현재 진행한 학습
      'amp': amp.state_dict()
    }, f'{self.checkpoint_path}/{self.model_name}.pth')
    model.cuda()

def meena_dataset(config, tokenizer, finetune_dataset):
  cache_data_path = f'{config.cache_path}/{config.model_name}.pickle'
  cache_dir_path= os.path.dirname(cache_data_path)

  if os.path.exists(cache_data_path): # 캐시 데이터가 존재하는 경우
    dataset = torch.load(cache_data_path)
    return dataset
  else: # 캐시 데이터가 없는 경우
    if not os.path.exists(cache_dir_path):
      os.makedirs(cache_dir_path) # 캐시 디렉토리 경로 생성

    dataset = finetune_dataset(tokenizer, config.max_seq_len, config.data_path,threshold=0.0)
    torch.save(dataset, cache_data_path) # 데이터 저장

    return dataset


def main():
  torch.manual_seed(9)
  torch.cuda.set_device(1)
  base_path = '..'

  log_dir = f'{base_path}/logs'
  config_path = f'{base_path}/config/meena-finetuning-config-v3.json'
  device = 'cuda:1' if torch.cuda.is_available() else 'cpu'

  # Config
  config = ModelConfig(config_path=config_path).get_config()

  # Tokenizer
  tokenizer = BertTokenizer(vocab_file=config.vocab_path, do_lower_case=False)

  # Dataset
  # dataset = DatasetForSeq2seqV2(tokenizer, config.max_seq_len, config.data_path)
  dataset = meena_dataset(config,tokenizer, DatasetForSeq2seqConversation)

  # Meena Model
  model = Meena(
          vocab_size = tokenizer.vocab_size,
          dim=config.dim,
          encoder_depth=config.encoder_depth,
          decoder_depth=config.decoder_depth,
          max_seq_len=config.max_seq_len,
          head_num=config.n_head,
          dropout=config.dropout_prob)

  if torch.cuda.is_available():
    model.cuda(1)

  checkpoint_path = f'{config.checkpoint_path}/{config.model_name}.pth'
  checkpoint = torch.load(checkpoint_path, map_location=device)
  model.load_state_dict(checkpoint['model_state_dict'])

  del checkpoint

  # optimizer = Adafactor(model.parameters())
  optimizer = Adafactor(model.parameters(),
                        scale_parameter=False, # (default: True) if True, learning rate is scaled by root mean square of parameter
                        relative_step=False, # (default: True) if True, time-dependent learning rate is computed
                        warmup_init=False, # (default: False) time-dependent learning rate computation depends on whether warm-up initialization is being used
                        lr=5e-5)
  # optimizer = AdamW(model.parameters(), lr=3e-4)

  if config.fp16:
    model, optimizer = amp.initialize(model, optimizer, opt_level=config.fp16_opt_level)

  # Pretraining Traniner
  trainer = MeenaTrainer(dataset, model, tokenizer,
                           model_name=config.model_name,
                           max_len=config.max_seq_len,
                           checkpoint_path=config.checkpoint_path,
                           train_batch_size=config.batch_size,
                           eval_batch_size=config.batch_size,
                           log_dir=log_dir,
                           fp16=config.fp16
                         )

  train_dataloader, eval_dataloader = trainer.build_dataloaders(train_test_split=0.1)

  trainer.train(epochs=config.epochs,
                train_dataloader=train_dataloader,
                eval_dataloader=eval_dataloader,
                optimizer=optimizer,
                log_steps=config.log_steps,
                ckpt_steps=config.ckpt_steps,
                gradient_accumulation_steps=config.gradient_accumulation_steps)


if __name__ == '__main__':
  main()
