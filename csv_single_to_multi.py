#!/usr/bin/env python3

import argparse
from collections import Counter
import pdb
import re
import sys
import time

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import torch.multiprocessing as mp

import data_producer

from multiprocessing import set_start_method

parser = argparse.ArgumentParser()
parser.add_argument("--model", type=str, default="", help='saved model file')
parser.add_argument("--train", type=str, default="", help="training file")
parser.add_argument("--save", type=str, default="csv.pth.tar", help="saved model filename")
parser.add_argument("--size", type=int, default=300, help="word embedding dimension")
parser.add_argument("--window", type=int, default=5, help="context window size")
parser.add_argument("--sample", type=float, default=1e-4, help="subsample threshold")
parser.add_argument("--negative", type=int, default=10, help="number of negative samples")
parser.add_argument("--delta", type=float, default=0.15, help="create new sense for a type if similarity lower than this value.")
parser.add_argument("--min_count", type=int, default=5, help="minimum frequency of a word")
parser.add_argument("--processes", type=int, default=4, help="number of processes")
parser.add_argument("--num_workers", type=int, default=6, help="number of workers for data processsing")
parser.add_argument("--iter", type=int, default=5, help="number of iterations")
parser.add_argument("--lr", type=float, default=-1.0, help="initial learning rate")
parser.add_argument("--batch_size", type=int, default=100, help="(max) batch size")
parser.add_argument("--cuda", action='store_true', default=False, help="enable cuda")
parser.add_argument("--multi_proto", action='store_true', default=False, help="True: multi-prototype, False:single-prototype")

MAX_SENT_LEN = 1000

# Build the vocabulary.
def file_split(f, delim=' \t\n', bufsize=1024):
    prev = ''
    while True:
        s = f.read(bufsize)
        if not s:
            break
        tokens = re.split('['+delim+']{1,}', s)
        if len(tokens) > 1:
            yield prev + tokens[0]
            prev = tokens[-1]
            for x in tokens[1:-1]:
                yield x
        else:
            prev += s
    if prev:
        yield prev

def build_vocab(args):
    #train_file = open(args.train, 'r')
    vocab = Counter()
    word_count = 0
    for word in file_split(open(args.train)):
        vocab[word] += 1
        word_count += 1
        if word_count % 10000 == 0:
            sys.stdout.write('%d\r' % len(vocab))
    freq = {k:v for k,v in vocab.items() if v >= args.min_count}
    word_count = sum([freq[k] for k in freq])
    word_list = sorted(freq, key=freq.get, reverse=True)
    word2idx = {}
    for i,w in enumerate(word_list):
        word2idx[w] = i

    print("Vocab size: %ld" % len(word2idx))
    print("Words in train file: %ld" % word_count)
    vars(args)['vocab_size'] = len(word2idx)
    vars(args)['train_words'] = word_count

    return word2idx, word_list, freq


class CSV(nn.Module):
    def __init__(self, args):
        super(CSV, self).__init__()
        self.global_embs = nn.Embedding(args.vocab_size+1, args.size, padding_idx=args.vocab_size, sparse=True)
        self.sense_embs = nn.Embedding(args.vocab_size*2, args.size, sparse=True)
        self.word2sense = [ [i] for i in range(args.vocab_size) ]
        self.ctx_weight = torch.nn.Parameter(torch.zeros(2*args.window, args.size))

        self.global_embs.weight.data.uniform_(-0.5/args.size, 0.5/args.size)
        #self.sense_embs.weight.data.uniform_(-0.5/args.size, 0.5/args.size)
        self.sense_embs.weight.data.zero_()
        self.ctx_weight.data.normal_(0,1)

        self.n_senses = args.vocab_size
        self.sense_capacity = args.vocab_size*2
        self.batch_size = args.batch_size
        self.size = args.size
        self.window = args.window
        self.negative = args.negative
        self.pad_idx = args.vocab_size

    def get_context_feats(self, ctx_type_indices):
        ctx_type_embs = self.global_embs(ctx_type_indices)
        ctx_feats = torch.sum(ctx_type_embs * self.ctx_weight, 1)
        return ctx_feats

    def get_possible_sense_embs(self, type_indices, cuda=True):
        sense_indices = []
        sense2idx = {}
        for type_id in type_indices:
            for s_id in self.word2sense[type_id]:
                if s_id not in sense2idx:
                    sense2idx[s_id] = len(sense_indices)
                    sense_indices.append( s_id )
        sense_indices = np.array(sense_indices)

        if cuda:
            sense_embs = self.sense_embs(Variable(torch.LongTensor(sense_indices).cuda()))
        else:
            sense_embs = self.sense_embs(Variable(torch.LongTensor(sense_indices)))

        return sense2idx, sense_embs.cpu().data.numpy()

    def add_sense_embs(self, created_sense_embs):
        n_created = np.array(created_sense_embs).shape[0]
        if n_created == 0:
            return
        created_sense_embs = torch.FloatTensor(np.array(created_sense_embs))
        self.sense_embs.weight.data[self.n_senses:self.n_senses+n_created, :] = created_sense_embs
        self.n_senses += n_created

        # max number of created_sense_emb = batch_size
        # reallocate sense_embs if needed to ensure enough capacity.
        if self.n_senses + self.batch_size > self.sense_capacity:
            new_capacity = self.sense_capacity * 3 // 2
            new_embs = nn.Embedding(new_capacity, self.size, sparse=True)
            new_embs.weight.data[:self.n_senses, :] = self.sense_embs.weight.data[:self.n_senses, :]
            self.sense_embs = new_embs
            #self.sense_embs = self.sense_embs.cuda()
            self.sense_capacity = new_capacity

            return True
        return False

    def forward(self, data, with_neg=True):
        ctx_type_indices = data[:, 0:2*self.window]
        pos_sense_idx = data[:, 2*self.window+1]
        if with_neg:
            neg_sense_indices = data[:, 2*self.window+2:2*self.window+2+self.negative]
            neg_mask = data[:, 2*self.window+2+self.negative:].float()

        ctx_type_embs = self.global_embs(ctx_type_indices)
        pos_sense_embs = self.sense_embs(pos_sense_idx)
        if with_neg:
            neg_sense_embs = self.sense_embs(neg_sense_indices)

        ctx_feats = torch.sum(ctx_type_embs * self.ctx_weight, 1, keepdim=True)

        # Neg Log Likelihood
        pos_ips = torch.sum(ctx_feats[:,0,:] * pos_sense_embs, 1)
        pos_loss = torch.sum( -F.logsigmoid(torch.clamp(pos_ips,max=10,min=-10)))
        if with_neg:
            neg_ips = torch.bmm(neg_sense_embs, ctx_feats.permute(0,2,1))[:,:,0]
            neg_loss = torch.sum( -F.logsigmoid(torch.clamp(-neg_ips,max=10,min=-10)) * neg_mask )
            return pos_loss + neg_loss
        else:
            return pos_loss


# Initialize model.
def init_net(args):
    if args.lr == -1.0:
        vars(args)['lr'] = 0.05
    return CSV(args)

def save_model(filename, model, args, word2idx):
    torch.save({
        'word2idx':word2idx,
        'args':args,
        'word2sense': model.word2sense,
        'n_senses': model.n_senses,
        'params': model.state_dict()
    }, filename)
    #}, 'test.pth.tar')

def load_model(filename):
    checkpoint = torch.load(filename)
    word2idx = checkpoint['word2idx']
    args = checkpoint['args']
    model = CSV(args)
    if args.cuda:
        model.cuda()

    model.global_embs.weight.data = checkpoint['params']['global_embs.weight']
    model.sense_embs.weight.data = checkpoint['params']['sense_embs.weight']
    model.ctx_weight.data = checkpoint['params']['ctx_weight']
    model.word2sense = checkpoint['word2sense']
    model.n_senses = checkpoint['n_senses']

    return model, word2idx

# Training
def train_process_sent_producer(p_id, data_queue, word_count_actual, word_list, word2idx, freq, args):
    n_proc = 1 if args.stage == 2 else args.processes
    N = 1 if args.stage == 2 else args.iter
    neg = 0 if args.stage == 2 else args.negative

    if args.negative > 0:
        table_ptr_val = data_producer.init_unigram_table(word_list, freq, args.train_words)

    train_file = open(args.train)
    file_pos = args.file_size * p_id // n_proc
    train_file.seek(file_pos, 0)
    while True:
        try:
            train_file.read(1)
        except UnicodeDecodeError:
            file_pos -= 1
            train_file.seek(file_pos, 0)
        else:
            train_file.seek(file_pos, 0)
            break

    batch_count = 0
    batch_placeholder = np.zeros((args.batch_size, 2*args.window+2+2*neg), 'int64')

    for it in range(N):
        train_file.seek(file_pos, 0)

        last_word_cnt = 0
        word_cnt = 0
        sentence = []
        prev = ''
        eof = False
        while True:
            #if word_cnt > args.train_words / n_proc:
            if eof or train_file.tell() > file_pos + args.file_size / n_proc:
                break

            while True:
                s = train_file.read(1)
                if not s:
                    eof = True
                    break
                #elif s == ' ':
                elif s == ' ' or s == '\t':
                    if prev in word2idx:
                        sentence.append(prev)
                    prev = ''
                    if len(sentence) >= MAX_SENT_LEN:
                        break
                elif s == '\n':
                    if prev in word2idx:
                        sentence.append(prev)
                    prev = ''
                    break
                else:
                    prev += s

            if len(sentence) > 0:
                sent_id = [ word2idx[word] for word in sentence ]

                '''
                # subsampling
                sent_id = []
                if args.sample != 0:
                    sent_len = len(sentence)
                    i = 0
                    while i < sent_len:
                        word = sentence[i]
                        f = freq[word] / args.train_words
                        pb = (np.sqrt(f / args.sample) + 1) * args.sample / f;

                        if pb > np.random.random_sample():
                            sent_id.append( word2idx[word] )
                        i += 1
                '''

                if len(sent_id) < 2:
                    word_cnt += len(sentence)
                    sentence.clear()
                    continue

                next_random = (2**24) * np.random.randint(0, 2**24) + np.random.randint(0, 2**24)
                chunk = data_producer.cbow_producer(sent_id, len(sent_id), table_ptr_val, args.window,
                            neg, args.vocab_size, args.batch_size, next_random)

                chunk_pos = 0
                while chunk_pos < chunk.shape[0]:
                    remain_space = args.batch_size - batch_count
                    remain_chunk = chunk.shape[0] - chunk_pos

                    if remain_chunk < remain_space:
                        take_from_chunk = remain_chunk
                    else:
                        take_from_chunk = remain_space

                    batch_placeholder[batch_count:batch_count+take_from_chunk, :] = chunk[chunk_pos:chunk_pos+take_from_chunk, :]
                    batch_count += take_from_chunk

                    if batch_count == args.batch_size:
                        data_queue.put(batch_placeholder)
                        batch_count = 0

                    chunk_pos += take_from_chunk

                word_cnt += len(sentence)
                if word_cnt - last_word_cnt > 10000:
                    with word_count_actual.get_lock():
                        word_count_actual.value += word_cnt - last_word_cnt
                    last_word_cnt = word_cnt
                sentence.clear()

        with word_count_actual.get_lock():
            word_count_actual.value += word_cnt - last_word_cnt
        print(p_id, it, file_pos, train_file.tell(), args.file_size)
    if batch_count > 0:
        data_queue.put(batch_placeholder[:batch_count,:])
    data_queue.put(None)
    print(p_id, file_pos, train_file.tell(), args.file_size)

def train_process(p_id, word_count_actual, word2idx, word_list, freq, args, model):
    data_queue = mp.SimpleQueue()
    #data_queue = mp.Queue(100000)

    #optimizer = optim.SGD(model.parameters(), lr=args.lr)
    lr = args.lr
    optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=lr)

    t = mp.Process(target=train_process_sent_producer, args=(p_id, data_queue, word_count_actual, word_list, word2idx, freq, args))
    t.start()

    n_iter = 1 if args.stage == 2 else args.iter
    # get from data_queue and feed to model
    prev_word_cnt = 0
    while True:
        chunk = data_queue.get()
        if chunk is None:
            break
        else:
            # lr anneal & output
            if word_count_actual.value - prev_word_cnt > 10000:
                if args.lr_anneal:
                    lr = args.lr * (1 - word_count_actual.value / (n_iter * args.train_words))
                    if lr < 0.0001 * args.lr:
                        lr = 0.0001 * args.lr
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr

                sys.stdout.write("\rAlpha: %0.8f, Progess: %0.2f, Words/sec: %f, word_cnt: %d" % (lr, word_count_actual.value / (n_iter * args.train_words) * 100, word_count_actual.value / (time.monotonic() - args.t_start), word_count_actual.value))
                sys.stdout.flush()
                prev_word_cnt = word_count_actual.value

            if args.stage == 1:
                if args.cuda:
                    data = Variable(torch.LongTensor(chunk).cuda(), requires_grad=False)
                else:
                    data = Variable(torch.LongTensor(chunk), requires_grad=False)

                optimizer.zero_grad()
                loss = model(data)
                loss.backward()
                optimizer.step()
                model.global_embs.weight.data[args.vocab_size].fill_(0)

            elif args.stage == 2:
                if args.cuda:
                    data = Variable(torch.LongTensor(chunk).cuda(), requires_grad=False)
                else:
                    data = Variable(torch.LongTensor(chunk), requires_grad=False)
                context_feats = model.get_context_feats(data[:, :2*args.window])
                context_feats = context_feats.cpu().data.numpy()
                sense2idx, sense_embs = model.get_possible_sense_embs(chunk[:, 2*args.window+1].tolist())
                zero = np.zeros((chunk.shape[0], args.size),'float32')
                '''
                vv = []
                bb = []
                for iii in range(100):
                    bb.append(np.dot(context_feats[iii], sense_embs[sense2idx[chunk[iii,-1]]]))
                    cos = np.dot(context_feats[iii], sense_embs[sense2idx[chunk[iii,-1]]]) / np.linalg.norm(context_feats[iii]) / np.linalg.norm(sense_embs[sense2idx[chunk[iii,-1]]])
                    vv.append(cos)
                pdb.set_trace()
                '''

                chunk, created_sense_embs = data_producer.create_n_select_sense(chunk, context_feats, sense2idx,
                                                np.concatenate((sense_embs,zero),0), model.word2sense, chunk.shape[0],
                                                sense_embs.shape[0], args.size, args.window, args.delta, model.n_senses)
                if model.add_sense_embs(created_sense_embs):
                    print("\nexapnded sense_embs: %d" % model.n_senses)
                    if args.cuda:
                        model.cuda()
                    optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=lr)

                # feed selected_sense_ids to model
                if chunk.shape[0] > 0:
                    if args.cuda:
                        data = Variable(torch.LongTensor(chunk).cuda(), requires_grad=False)
                    else:
                        data = Variable(torch.LongTensor(chunk), requires_grad=False)

                    optimizer.zero_grad()
                    loss = model(data, False)
                    loss.backward()
                    optimizer.step()

            elif args.stage == 3:
                if args.cuda:
                    data = Variable(torch.LongTensor(chunk).cuda(), requires_grad=False)
                else:
                    data = Variable(torch.LongTensor(chunk), requires_grad=False)

                type_ids = chunk[:, 2*args.window+1:2*args.window+2+2*args.negative]
                type_ids = np.reshape(type_ids, (type_ids.shape[0] * type_ids.shape[1]))
                sense2idx, sense_embs = model.get_possible_sense_embs(type_ids.tolist())

                # get type_idx from chunk, and do sense selection here.
                context_feats = model.get_context_feats(data[:, :2*args.window])
                context_feats = context_feats.cpu().data.numpy()

                chunk = data_producer.select_sense(chunk, context_feats, sense2idx, sense_embs,
                            model.word2sense, chunk.shape[0], args.size, args.window, args.negative)

                if args.cuda:
                    data = Variable(torch.LongTensor(chunk).cuda(), requires_grad=False)
                else:
                    data = Variable(torch.LongTensor(chunk), requires_grad=False)

                optimizer.zero_grad()
                loss = model(data)
                loss.backward()
                optimizer.step()
                model.global_embs.weight.data[args.vocab_size].fill_(0)
    t.join()

if __name__ == '__main__':
    set_start_method('forkserver')

    args = parser.parse_args()
    print("Starting training using file %s" % args.train)
    train_file = open(args.train)
    train_file.seek(0, 2)
    vars(args)['file_size'] = train_file.tell()

    word_count_actual = mp.Value('L', 0)

    word2idx, word_list, freq = build_vocab(args)

    '''
    model = init_net(args)
    model.share_memory()
    if args.cuda:
        model.cuda()


    # stage 1, learn robust context representation.
    vars(args)['stage'] = 1
    print("Stage 1")
    vars(args)['lr_anneal'] = True
    vars(args)['t_start'] = time.monotonic()
    processes = []
    for p_id in range(args.processes):
        p = mp.Process(target=train_process, args=(p_id, word_count_actual, word2idx, word_list, freq, args, model))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()
    del processes
    print("\nStage 1, ", time.monotonic() - args.t_start, " secs ", word_count_actual.value)
    '''

    model, _ = load_model(args.model)

    if args.multi_proto:
        # stage 2, create new sense in a non-parametric way.
        # Freeze model paramters except sense_embs, and use only 1 process to prevent race condition
        model.global_embs.requires_grad = False
        model.ctx_weight.requires_grad = False
        vars(args)['stage'] = 2
        print("\nStage 2")
        vars(args)['lr'] = args.lr * 0.001
        vars(args)['lr_anneal'] = False
        word_count_actual.value = 0
        vars(args)['t_start'] = time.monotonic()
        train_process(0, word_count_actual, word2idx, word_list, freq, args, model)
        #p = mp.Process(target=train_process, args=(0, word_count_actual, word2idx, word_list, freq, args, model))
        #p.start()
        #p.join()

        # resize sense_embs
        new_embs = nn.Embedding(model.n_senses, args.size, sparse=True)
        new_embs.weight.data[:model.n_senses, :] = model.sense_embs.weight.data[:model.n_senses, :]
        model.sense_embs = new_embs
        if args.cuda:
            model.cuda()
        print("\nStage 2, ", time.monotonic() - args.t_start, " secs")
        print("Current # of senses: %d" % model.n_senses)

        # stage 3, no more sense creation.
        model.global_embs.requires_grad = True
        model.ctx_weight.requires_grad = True
        vars(args)['stage'] = 3
        print("\nBegin stage 3")
        word_count_actual.value = 0
        vars(args)['t_start'] = time.monotonic()
        processes = []
        for p_id in range(args.processes):
            p = mp.Process(target=train_process, args=(p_id, word_count_actual, word2idx, word_list, freq, args, model))
            p.start()
            processes.append(p)

        for p in processes:
            p.join()

        print("\nStage 3, ", time.monotonic() - args.t_start, " secs")

    # save model
    filename = args.save
    if not filename.endswith('.pth.tar'):
        filename += '.pth.tar'
    save_model(filename, model, args, word2idx)
    print("")

