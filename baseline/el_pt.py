import collections
import json
import logging
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from random import choice

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from pytorch_pretrained_bert import BertAdam
from pytorch_pretrained_bert.modeling import BertPreTrainedModel, BertModel
from tqdm import tqdm

from configuration.config import data_dir, bert_vocab_path, bert_model_path, bert_data_path

min_count = 2
mode = 0
hidden_size = 768
epoch_num = 10
batch_size = 64

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt='%m/%d/%y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

id2kb = {}
for l in (Path(data_dir) / 'kb_data').open():
    _ = json.loads(l)
    subject_id = _['subject_id']
    subject_alias = list(set([_['subject']] + _.get('alias', [])))
    subject_alias = [sa.lower() for sa in subject_alias]
    subject_desc = ''
    for i in _['data']:
        if '摘要' in i['predicate']:
            subject_desc = i['object']
            break
        else:
            subject_desc += f'{i["predicate"]}:{i["object"]}\n'

    # subject_desc = '\n'.join(f'{i["predicate"]}: {i["object"]}' for i in _['data'])
    subject_desc = subject_desc[:300].lower()
    if subject_desc:
        id2kb[subject_id] = {'subject_alias': subject_alias, 'subject_desc': subject_desc}

kb2id = defaultdict(list)  # subject: [sid1, sid2,...]
for i, j in id2kb.items():
    for k in j['subject_alias']:
        kb2id[k].append(i)

train_data = []
for l in (Path(data_dir) / 'train.json').open():
    _ = json.loads(l)
    train_data.append({
        'text': _['text'].lower(),
        'mention_data': [(x['mention'], int(x['offset']), x['kb_id'])
                         for x in _['mention_data'] if x['kb_id'] != 'NIL']
    })

if not (Path(data_dir) / 'all_chars_me.json').exists():
    chars = {}
    for d in tqdm(iter(id2kb.values())):
        for c in d['subject_desc']:
            chars[c] = chars.get(c, 0) + 1
    for d in tqdm(iter(train_data)):
        for c in d['text']:
            chars[c] = chars.get(c, 0) + 1
    chars = {i: j for i, j in chars.items() if j >= min_count}
    id2char = {i + 2: j for i, j in enumerate(chars)}
    char2id = {j: i for i, j in id2char.items()}
    json.dump([id2char, char2id], (Path(data_dir) / 'all_chars_me.json').open('w'))
else:
    id2char, char2id = json.load((Path(data_dir) / 'all_chars_me.json').open())

if not (Path(data_dir) / 'random_order_train.json').exists():
    random_order = list(range(len(train_data)))
    np.random.shuffle(random_order)
    json.dump(
        random_order,
        (Path(data_dir) / 'random_order_train.json').open('w'),
        indent=4
    )
else:
    random_order = json.load((Path(data_dir) / 'random_order_train.json').open())

dev_data = [train_data[j] for i, j in enumerate(random_order) if i % 9 == mode]
train_data = [train_data[j] for i, j in enumerate(random_order) if i % 9 != mode]


def seq_padding(X):
    ML = max(map(len, X))
    return [list(x) + [0] * (ML - len(x)) for x in X]


def load_vocab(vocab_file):
    """Loads a vocabulary file into a dictionary."""
    vocab = collections.OrderedDict()
    index = 0
    with open(vocab_file, "r", encoding="utf-8") as reader:
        while True:
            token = reader.readline()
            if not token:
                break
            token = token.strip()
            vocab[token] = index
            index += 1
    return vocab


bert_vocab = load_vocab(bert_vocab_path)


class data_generator:
    def __init__(self, data, batch_size=64):
        self.data = data
        self.batch_size = batch_size
        self.steps = len(self.data) // self.batch_size
        if len(self.data) % self.batch_size != 0:
            self.steps += 1

    def __len__(self):
        return self.steps

    def __iter__(self):
        idxs = list(range(len(self.data)))
        np.random.shuffle(idxs)
        X1, X2, S1, S2, Y, T, X1_MASK, X2_MASK = [], [], [], [], [], [], [], []
        for i in idxs:
            d = self.data[i]
            text = d['text']
            x1 = [bert_vocab.get(c, bert_vocab.get('[UNK]')) for c in text]
            x1_mask = [1] * len(x1)
            s1, s2 = np.zeros(len(text)), np.zeros(len(text))
            mds = {}
            for md in d['mention_data']:
                if md[0] in kb2id:
                    j1 = md[1]
                    j2 = md[1] + len(md[0])
                    s1[j1] = 1
                    s2[j2 - 1] = 1
                    mds[(j1, j2)] = (md[0], md[2])

            if mds:
                j1, j2 = choice(list(mds.keys()))
                y = np.zeros(len(text))
                y[j1:j2] = 1
                x2 = choice(kb2id[mds[(j1, j2)][0]])
                if x2 == mds[(j1, j2)][1]:
                    t = [1]
                else:
                    t = [0]
                x2 = id2kb[x2]['subject_desc']
                x2 = [bert_vocab.get(c, bert_vocab.get('[UNK]')) for c in x2]
                x2_mask = [1] * len(x2)
                X1.append(x1)
                X2.append(x2)
                S1.append(s1)
                S2.append(s2)
                Y.append(y)
                T.append(t)
                X1_MASK.append(x1_mask)
                X2_MASK.append(x2_mask)
                if len(X1) == self.batch_size or i == idxs[-1]:
                    X1 = torch.tensor(seq_padding(X1), dtype=torch.long)  # [b,s1]
                    X2 = torch.tensor(seq_padding(X2), dtype=torch.long)  # [b,s2]
                    S1 = torch.tensor(seq_padding(S1), dtype=torch.float32)  # [b,s1]
                    S2 = torch.tensor(seq_padding(S2), dtype=torch.float32) #[b,s1]
                    Y = torch.tensor(seq_padding(Y), dtype=torch.float32) #[b,s1]
                    T = torch.tensor(T, dtype=torch.float32) #[b,1]
                    X1_MASK = torch.tensor(seq_padding(X1_MASK), dtype=torch.long)
                    X2_MASK = torch.tensor(seq_padding(X2_MASK), dtype=torch.long)
                    X1_SEG = torch.zeros(*X1.size(), dtype=torch.long)
                    X2_SEG = torch.zeros(*X2.size(),dtype=torch.long)

                    yield [X1, X2, S1, S2, Y, T, X1_MASK, X2_MASK, X1_SEG, X2_SEG]
                    X1, X2, S1, S2, Y, T, X1_MASK, X2_MASK = [], [], [], [], [], [], [], []


class SubjectModel(BertPreTrainedModel):
    def __init__(self, config):
        super(SubjectModel, self).__init__(config)
        self.bert = BertModel(config)
        self.linear1 = nn.Linear(in_features=hidden_size, out_features=1)
        self.linear2 = nn.Linear(in_features=hidden_size, out_features=1)

    def forward(self, x1_ids, x1_segments, x1_mask):
        x1_encoder_layers, x1_pooled_out = self.bert(x1_ids, x1_segments, x1_mask, output_all_encoded_layers=False)

        ps1 = torch.sigmoid(self.linear1(x1_encoder_layers).squeeze(-1))
        ps2 = torch.sigmoid(self.linear2(x1_encoder_layers).squeeze(-1))

        return ps1, ps2, x1_encoder_layers, x1_pooled_out


class ObjectModel(BertPreTrainedModel):
    def __init__(self, config):
        super(ObjectModel, self).__init__(config)
        self.bert = BertModel(config)
        self.w = nn.Parameter(nn.init.xavier_normal(torch.empty(hidden_size + 1, hidden_size)))
        self.linear = nn.Linear(in_features=hidden_size * 3, out_features=1)

    def forward(self, x1, x1_h, x1_mask, y, x2_ids, x2_segments, x2_mask):
        x1 = torch.cat([x1, y.unsqueeze(2)], dim=-1)  # [b,s,h] [b,s,1] -> [b,s,h+1]

        x2, x2_h = self.bert(x2_ids, x2_segments, x2_mask, output_all_encoded_layers=False)

        x1_mask = 1 - x1_mask.byte()
        x2_mask = 1 - x2_mask.byte()

        x1w = torch.matmul(x1, self.w)
        a = torch.bmm(x1w, x2.permute(0, 2, 1))  # [b,s1,s2]
        a.masked_fill_(x2_mask.unsqueeze(1), -1e-5)  # [b,s2]->[b,1,s2]
        a = F.softmax(a, dim=-1)
        x12 = torch.bmm(a, x2)  # [b,s1,s2]*[b,s2,h]->[b,s1,h]
        x12 = F.max_pool1d(x12.masked_fill(x1_mask.unsqueeze(2), -1e5).permute(0, 2, 1), kernel_size=x12.size(1))
        x12 = x12.squeeze(2)

        h = torch.cat([x1_h, x2_h, x12], dim=1)  # [b,3*h]

        o = torch.sigmoid(self.linear(h))

        return o, x1_mask, x2_mask


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
n_gpu = torch.cuda.device_count()
if n_gpu>1:
    logger.info(f'use {n_gpu} gpu')

subject_model = SubjectModel.from_pretrained(pretrained_model_name_or_path=bert_model_path, cache_dir=bert_data_path)
object_model = ObjectModel.from_pretrained(pretrained_model_name_or_path=bert_model_path, cache_dir=bert_data_path)

subject_model.to(device)
object_model.to(device)
if n_gpu > 1:
    torch.cuda.manual_seed_all(42)

    logger.info(f'let us use {n_gpu} gpu')
    subject_model = torch.nn.DataParallel(subject_model)
    object_model = torch.nn.DataParallel(object_model)

# loss
b_loss_func = nn.BCELoss(reduction='none')
b2_loss_func = nn.BCELoss()

# optim
param_optimizer = list(subject_model.named_parameters()) + list(object_model.named_parameters())
no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
optimizer_grouped_parameters = [
    {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
    {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
]

learning_rate = 5e-5
warmup_proportion = 0.1
num_train_optimization_steps = len(train_data) // batch_size * epoch_num
logger.info(f'num_train_optimization: {num_train_optimization_steps}')

optimizer = BertAdam(optimizer_grouped_parameters,
                     lr=learning_rate,
                     warmup=warmup_proportion,
                     t_total=num_train_optimization_steps)


def extract_items(text_in):
    _s = [bert_vocab.get(c, bert_vocab.get('[UNK]')) for c in text_in]
    _input_mask = [1] * len(_s)
    _s = torch.tensor([_s], dtype=torch.long, device=device)  # [1,s1]
    _input_mask = torch.tensor([_input_mask], dtype=torch.long, device=device)
    _segment_ids = torch.zeros(*_s.size(), dtype=torch.long, device=device)

    with torch.no_grad():
        _k1, _k2, _x1_hs, _x1_h = subject_model(_s, _segment_ids, _input_mask)  # _k1:[1,s]
        _k1 = _k1[0,:].detach().cpu().numpy()
        _k2 = _k2[0,:].detach().cpu().numpy()
        _k1, _k2 = np.where(_k1>0.5)[0], np.where(_k2>0.5)[0]

    _subjects = []
    if len(_k1) and len(_k2):
        for i in _k1:
            j = _k2[_k2>=i]
            if len(j)>0:
                j= j[0]
                _subject = text_in[i:j+1]
                _subjects.append((_subject, i,j+1))
    if _subjects:
        R= []
        _X2, _X2_MASK,_Y = [],[],[]
        _S,_IDXS = [],{}
        for _s in _subjects:
            _y = np.zeros(len(text_in))
            _y[_s[1]:_s[2]] = 1
            _IDXS[_s] = kb2id.get(_s[0], [])
            for i in _IDXS[_s]:
                _x2 = id2kb[i]['subject_desc']
                _x2 = [bert_vocab.get(c, bert_vocab.get('[UNK]')) for c in _x2]
                _x2_mask = [1]*len(_x2)
                _X2.append(_x2)
                _X2_MASK.append(_x2_mask)
                _Y.append(_y)
                _S.append(_s)
        if _X2:
            _X2 = torch.tensor(seq_padding(_X2), dtype=torch.long, device=device)  # [b,s2]
            _X2_MASK = torch.tensor(seq_padding(_X2_MASK), dtype=torch.long, device=device)
            _X2_SEG = torch.zeros(*_X2.size(), dtype=torch.long, device=device)
            _Y = torch.tensor(seq_padding(_Y), dtype=torch.float32)
            _X1_HS = _x1_hs.expand(_X2.size(0),-1,-1)  #[b,s1]
            _X1_H = _x1_h.expand(_X2.size(0),-1)  #[b,s1]
            _input_mask = _input_mask.expand(_X2.size(0),-1)  #[b,s1]

            with torch.no_grad():
                _o, _, _ = object_model(_X1_HS,_X1_H,_input_mask,_Y,_X2,_X2_SEG,_X2_MASK)  # _o:[b,1]
                _o = _o.detach().cpu().numpy()
                for k, v in groupby(zip(_S,_o), key=lambda x:x[0]):
                    v = np.array([j[1] for j in v])
                    assert len(_IDXS[k]) == len(v)
                    kbid = _IDXS[k][np.argmax(v)]
                    R.append((k[0],k[1],kbid))
        return R
    else:
        return []


err_log = (Path(data_dir) / 'err_log.json').open('w')
err_dict = defaultdict(list)

best_score = 0
best_epoch = 0
train_D = data_generator(train_data)
for e in range(epoch_num):
    subject_model.train()
    object_model.train()
    batch_idx = 0
    tr_total_loss = 0
    dev_total_loss = 0

    for batch in train_D:
        batch_idx += 1
        if batch_idx > 1:
            break

        batch = tuple(t.to(device) for t in batch)
        X1, X2, S1, S2, Y, T, X1_MASK, X2_MASK, X1_SEG, X2_SEG = batch
        pred_s1, pred_s2, x1_hs, x1_h = subject_model(X1, X1_SEG, X1_MASK)
        pred_o, x1_mask_, x2_mask_ = object_model(x1_hs, x1_h, X1_MASK, Y, X2, X2_SEG, X2_MASK)

        s1_loss = b_loss_func(pred_s1, S1)  # [b,s]
        s2_loss = b_loss_func(pred_s2, S2)

        s1_loss.masked_fill_(x1_mask_, 0)
        s2_loss.masked_fill_(x1_mask_, 0)

        total_ele = X1.size(0) * X1.size(1) - torch.sum(x1_mask_)
        s1_loss = torch.sum(s1_loss) / total_ele
        s2_loss = torch.sum(s2_loss) / total_ele

        po_loss = b2_loss_func(pred_o, T)

        tmp_loss = (s1_loss + s2_loss) + po_loss

        if n_gpu > 1:
            tmp_loss = tmp_loss.mean()

        tmp_loss.backward()

        optimizer.step()
        optimizer.zero_grad()

        tr_total_loss += tmp_loss.item()
        if batch_idx % 100 == 0:
            logger.info(f'Epoch:{e} - batch:{batch_idx}/{train_D.steps} - loss: {tr_total_loss / batch_idx:.8f}')

    subject_model.eval()
    object_model.eval()
    A, B, C = 1e-10, 1e-10, 1e-10
    for d in tqdm(iter(dev_data)):
        R = set(extract_items(d['text']))
        T = set(d['mention_data'])
        A += len(R & T)
        B += len(R)
        C += len(T)

        if R != T:
            err_dict['err'].append({'text': d['text'],
                                    'mention_data': d['mention_data'],
                                    'predict': list(R)})

    f1, precision, recall = 2 * A / (B + C), A / B, A / C
    if f1 > best_score:
        best_score = f1
        best_epoch = e

        json.dump(err_dict, err_log, ensure_ascii=False)

        s_model_to_save = subject_model.module if hasattr(subject_model, 'module') else subject_model
        o_model_to_save = object_model.module if hasattr(object_model, 'module') else object_model

        torch.save(s_model_to_save.state_dict(), data_dir + '/subject_model.pt')
        torch.save(o_model_to_save.state_dict(), data_dir + '/object_model.pt')

        (Path(data_dir)/'subject_model_config.json').open('w').write(s_model_to_save.config.to_json_string())
        (Path(data_dir)/'object_model_config.json').open('w').write(o_model_to_save.config.to_json_string())

    logger.info(
        f'Epoch:{e}-precision:{precision:.4f}-recall:{recall:.4f}-f1:{f1:.4f} - best f1: {best_score:.4f} - best epoch:{best_epoch}')
