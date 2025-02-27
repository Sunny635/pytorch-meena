import warnings
warnings.filterwarnings("ignore")

import torch
from common.arg import ModelConfig
from model.meena import Meena
from transformers import BertTokenizer
from common.generate import top_p, top_k,sample_and_rank
import time


def get_encoder_input(tokenizer:BertTokenizer, input_str:list, config: ModelConfig):
    encoder_input = torch.tensor(tokenizer.encode(input_str,
                                                    add_special_tokens=False,
                                                    pad_to_max_length=True,
                                                    max_length=config.max_seq_len,
                                                    return_tensors='pt',
                                                    truncation=True))
    encoder_input_mask = encoder_input !=0
    return encoder_input, encoder_input_mask

def get_decoder_input(tokenizer:BertTokenizer, input_str:list, config: ModelConfig):
    decoder_input = torch.tensor(tokenizer.encode(input_str,
                                                    add_special_tokens=False,
                                                    max_length=config.max_seq_len,
                                                    return_tensors='pt',
                                                    truncation=True))
    return decoder_input

def get_next_token(logit, func, **args):
    next_token_ebedd = logit.squeeze()[-1]
    sampled_word = func(next_token_ebedd, **args)

    return sampled_word

def remove_pad_token(tokenizer:BertTokenizer, input_ids: torch.Tensor):
    pad_token_mask  = input_ids != tokenizer.pad_token_id
    removed_pad_input_ids = input_ids[pad_token_mask]
    return removed_pad_input_ids.tolist()

def make_new_source_input(tokenizer:BertTokenizer, target_input_ids: torch.Tensor, source_input_ids:torch.Tensor):
    list_target_input_ids = target_input_ids.tolist()[0]
    list_target_input_ids.append(tokenizer.sep_token_id)

    source_input_ids = remove_pad_token(tokenizer, source_input_ids[0])
    source_input_ids = source_input_ids + list_target_input_ids[1:]
    if source_input_ids[-127:][0] == tokenizer.cls_token_id:
        source_input_ids = source_input_ids[-127:]
    else:
        source_input_ids = [tokenizer.cls_token_id] + source_input_ids[-127:]

    source_input_str = tokenizer.decode(source_input_ids, clean_up_tokenization_spaces=True)

    return torch.tensor(source_input_ids), source_input_str

def print_output(out_ids, tokenizer:BertTokenizer):
    out_str = tokenizer.decode(out_ids)
    turn = 'B :'
    out_str = f'Meena: {out_str[4:]}'
    out_list = out_str.split(turn)

    if len(out_list)==1:
        print(out_str)
    elif len(out_list)>1:
        for item in out_list:
            print(item)
            time.sleep(1.1)

def main():

    config_path = '../config/meena-config.json'
    checkpoint_path = '../checkpoint/komeena-base-finetuning-v3.pth'

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    config = ModelConfig(config_path).get_config()

    tokenizer = BertTokenizer(config.vocab_path, do_lower_case=False)

    model = Meena(vocab_size=tokenizer.vocab_size,
                  dim=config.dim,
                  encoder_depth=config.encoder_depth,
                  decoder_depth=config.decoder_depth,
                  max_seq_len=config.max_seq_len,
                  head_num=config.n_head,
                  dropout=config.dropout_prob)

    checkpoint = torch.load(checkpoint_path, map_location=device)
    model.load_state_dict(checkpoint['model_state_dict'])
    del checkpoint

    model.eval()

    # Meta data for conversation
    meta_data = '[CLS] '

    # Start of chat with Meena
    target_str = '[CLS] B: '
    print('Meena에게 말을 건네세요: ')

    count = 0
    min_len = 15

    while True:
        user_query = input('A : ')
        user_query = f'A : {user_query}'

        if count == 0:
            source_str = meta_data + user_query + ' [SEP] '
        else:
            user_query_ids = tokenizer.encode(user_query, add_special_tokens=False, max_length=config.max_seq_len,
                                              truncation=True)
            user_query_ids.append(tokenizer.sep_token_id)
            user_query_ids = torch.tensor(user_query_ids)
            _, source_str = make_new_source_input(tokenizer, user_query_ids.unsqueeze(0), source_input_ids)
        source_input_ids, source_input_mask = get_encoder_input(tokenizer=tokenizer, input_str=source_str,
                                                                config=config)
        target_input_ids = get_decoder_input(tokenizer=tokenizer, input_str=target_str, config=config)

        # Sentence completed normally
        is_complete = False

        for _ in range(config.max_seq_len):
            logit, _ = model(source_input_ids, target_input_ids, source_input_mask)
            sampled_word = get_next_token(logit, top_p)
            # sampled_word = get_next_token(logit, sample_and_rank, N=20, temperature=0.88, is_uniform_sample=False)

            if sampled_word == tokenizer.sep_token_id:
                if len(target_input_ids[0]) > min_len:
                    # print(f'{tokenizer.decode(target_input_ids[0], skip_special_tokens=True)}')
                    print_output(target_input_ids[0],tokenizer)
                    source_input_ids, source_str = make_new_source_input(tokenizer, target_input_ids, source_input_ids)
                    target_str = '[CLS] B: '
                    is_complete = True
                    break
                else:
                    addtional_target_str = ' [UNK] B: '
                    addtional_target_ids = tokenizer.encode(addtional_target_str, add_special_tokens=False)

                    target_input_ids = target_input_ids.tolist()
                    target_input_ids[0].append(addtional_target_ids[0])
                    target_input_ids = torch.tensor(target_input_ids)

            else:
                target_input_ids = target_input_ids.tolist()
                target_input_ids[0].append(sampled_word)
                target_input_ids = torch.tensor(target_input_ids)

        if is_complete == False:
            source_input_ids, source_str = make_new_source_input(tokenizer, target_input_ids, source_input_ids)
            target_str = '[CLS] B: '

        count += 1



if __name__=='__main__':
    main()







