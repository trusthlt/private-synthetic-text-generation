# import blobfile as bf
import json
import os

import datasets
import numpy as np
import psutil
import torch
from datasets import Dataset as Dataset2
from torch.utils.data import DataLoader, Dataset


def load_data_text(
        batch_size,
        seq_len,
        deterministic=False,
        data_args=None,
        model_emb=None,
        split='train',
        loaded_vocab=None,
        loop=True,
):
    """
    For a dataset, create a generator over (seqs, kwargs) pairs.

    Each seq is an (bsz, len, h) float tensor, and the kwargs dict contains zero or
    more keys, each of which map to a batched Tensor of their own.
    The kwargs dict can be used for some meta information.

    :param batch_size: the batch size of each returned pair.
    :param seq_len: the max sequence length (one-side).
    :param deterministic: if True, yield results in a deterministic order.
    :param data_args: including dataset directory, num of dataset, basic settings, etc.
    :param model_emb: loaded word embeddings.
    :param loaded_vocab: loaded word vocabs.
    :param loop: loop to get batch data or not.
    """

    print('#' * 30, '\nLoading text data...')

    training_data = get_corpus(data_args, seq_len, split=split, loaded_vocab=loaded_vocab)

    dataset = TextDataset(
        training_data,
        data_args,
        model_emb=model_emb
    )

    if split not in ['test', 'samples']:
        data_loader = DataLoader(
            dataset,
            batch_size=batch_size,
            num_workers=0,
        )
    else:
        data_loader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=0,
        )

    return data_loader


def helper_tokenize(sentence_lst, vocab_dict, seq_len):
    raw_datasets = Dataset2.from_dict(sentence_lst)

    def tokenize_function(examples):
        input_id_x = vocab_dict.encode_token(examples['src'])
        input_id_y = vocab_dict.encode_token(examples['trg'])
        result_dict = {'input_id_x': input_id_x, 'input_id_y': input_id_y}

        return result_dict

    tokenized_datasets = raw_datasets.map(
        tokenize_function,
        batched=True,
        num_proc=4,
        remove_columns=['src', 'trg'],
        load_from_cache_file=True,
        desc="Running tokenizer on dataset",
    )

    def merge_and_mask(group_lst):
        lst = []
        mask = []
        for i in range(len(group_lst['input_id_x'])):
            end_token = group_lst['input_id_x'][i][-1]
            src = group_lst['input_id_x'][i][:-1]
            trg = group_lst['input_id_y'][i][:-1]
            while len(src) + len(trg) > seq_len - 3:
                if len(src) > len(trg):
                    src.pop()
                elif len(src) < len(trg):
                    trg.pop()
                else:
                    src.pop()
                    trg.pop()
            src.append(end_token)
            trg.append(end_token)

            lst.append(src + [vocab_dict.sep_token_id] + trg)
            mask.append([0] * (len(src) + 1))
        group_lst['input_ids'] = lst
        group_lst['input_mask'] = mask
        return group_lst

    tokenized_datasets = tokenized_datasets.map(
        merge_and_mask,
        batched=True,
        num_proc=1,
        desc=f"merge and mask",
    )

    def pad_function(group_lst):
        max_length = seq_len
        group_lst['input_ids'] = _collate_batch_helper(group_lst['input_ids'], vocab_dict.pad_token_id, max_length)
        group_lst['input_mask'] = _collate_batch_helper(group_lst['input_mask'], 1, max_length)
        return group_lst

    lm_datasets = tokenized_datasets.map(
        pad_function,
        batched=True,
        num_proc=1,
        desc=f"padding",
    )

    raw_datasets = datasets.DatasetDict()
    raw_datasets['train'] = lm_datasets
    return raw_datasets


def get_corpus(data_args, seq_len, split='train', loaded_vocab=None):
    print('Loading dataset')

    sentence_lst = {'src': [], 'trg': []}

    path = os.path.join('..', 'data', data_args.dataset)

    if split == 'train':

        print('### Loading form the TRAIN set...')
        path = f'{path}/train.jsonl'
    elif split == 'valid':
        print('### Loading form the VALID set...')
        path = f'{path}/valid.jsonl'
    elif split == 'test':
        print('### Loading form the TEST set...')
        path = f'{path}/test.jsonl'
    elif split == 'samples':
        # include labels so labels carry over
        sentence_lst = {'src': [], 'trg': [], 'label': []}
        print('### Loading samples')
        # TODO make loading for sampling different
        path = f'{path}/samples.jsonl'

    else:
        assert False, "invalid split for dataset"

    with open(path, 'r') as f_reader:
        for row in f_reader:
            content = json.loads(row)
            sentence_lst['src'].append(content['src'].strip())
            sentence_lst['trg'].append(content['trg'].strip())
            if split == 'samples':
                sentence_lst['label'].append(content['label'])

    # get tokenizer.
    vocab_dict = loaded_vocab

    train_dataset = helper_tokenize(sentence_lst, vocab_dict, seq_len)
    return train_dataset


class TextDataset(Dataset):
    def __init__(self, text_datasets, data_args, model_emb=None):
        super().__init__()
        self.text_datasets = text_datasets
        self.length = len(self.text_datasets['train'])
        self.data_args = data_args
        self.model_emb = model_emb

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        with torch.no_grad():
            input_ids = self.text_datasets['train'][idx]['input_ids']
            hidden_state = self.model_emb(torch.tensor(input_ids))

            # obtain the input vectors, only used when word embedding is fixed (not trained end-to-end)
            arr = np.array(hidden_state, dtype=np.float32)

            out_kwargs = {}
            out_kwargs['input_ids'] = np.array(self.text_datasets['train'][idx]['input_ids'])
            out_kwargs['input_mask'] = np.array(self.text_datasets['train'][idx]['input_mask'])
            # labels need to carry over during sampling
            if 'label' in self.text_datasets.column_names['train']:
                out_kwargs['label'] = np.array(self.text_datasets['train'][idx].get('label', 0))

            return arr, out_kwargs


def _collate_batch_helper(examples, pad_token_id, max_length, return_mask=False):
    result = torch.full([len(examples), max_length], pad_token_id, dtype=torch.int64).tolist()
    mask_ = torch.full([len(examples), max_length], pad_token_id, dtype=torch.int64).tolist()
    for i, example in enumerate(examples):
        curr_len = min(len(example), max_length)
        result[i][:curr_len] = example[:curr_len]
        mask_[i][:curr_len] = [1] * curr_len
    if return_mask:
        return result, mask_
    return result
