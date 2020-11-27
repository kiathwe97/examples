# coding: utf-8
import argparse
import time
import math
import os
import torch
import torch.nn as nn
import torch.onnx

import data
import model

parser = argparse.ArgumentParser(description='PyTorch Wikitext-2 RNN/LSTM/GRU/Transformer Language Model')
parser.add_argument('--data', type=str, default='examples/word_language_model/data/wikitext-2',
                    help='location of the data corpus')
parser.add_argument("--name", type=str, default="", help="Name to identify model that is being trained")
parser.add_argument('--model', type=str, default='FNN',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU, Transformer)')
parser.add_argument('--emsize', type=int, default=10,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--epochs', type=int, default=200,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--dropout', type=float, default=0.0, metavar='N',
                    help='Dropout rate')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--log-interval', type=int, default=2, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str, default='model.pt',
                    help='path to save the final model')
parser.add_argument('--dry-run', action='store_true',
                    help='verify the code and the model')
parser.add_argument('--n', type=int, default = 8,
                    help='n in n-gram')
parser.add_argument("--optimizer", type=str, default="sgd", choices=["sgd", "adam", "rmsprops"], help=" Optimizer to use for learning, one of : sgd, adam or rmsprops")
args = parser.parse_args()

import torch
from torch.utils.tensorboard import SummaryWriter
writer = SummaryWriter(comment=args.name)

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

device = torch.device("cuda" if args.cuda else "cpu")

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data
eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, args.batch_size)
test_data = batchify(corpus.test, args.batch_size)
print(train_data)
###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)
if args.model == "FNN":
    model = model.FNNModel(args.emsize, ntokens, (args.nhid,), ngram=args.n, dropout=args.dropout).to(device)

criterion = nn.NLLLoss()

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h):
    """Wraps hidden states in new Tensors, to detach them from their history."""

    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)


# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i, ngram):
    return torch.narrow(source, 1, i, ngram), torch.narrow(source, 1, i+ngram, 1).view(-1)

def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0.
    ntokens = len(corpus.dictionary)
    with torch.no_grad():
      total_steps = data_source.size(1) - args.n -1
      for step_num in range(1, total_steps):
          data, targets = get_batch(test_data, step_num, args.n)
          data = data.to(device)
          targets = targets.to(device)
          output = model(data)
          total_loss += criterion(output, targets).item()
    return total_loss / total_steps

def train(optimizer):
    epoch_loss = 0.
    interval_loss = 0.
    start_time = time.time()
    ntokens = len(corpus.dictionary)

    # Turn on training mode which enables dropout.
    model.train()
    # Training for ngram case is lesser by :n(size of n-gram) - 1 for whole corpus because n-gram is only valid when predicting for (size-n)th word
    total_steps = train_data.size(1) - args.n -1
    for step_num in range(1, total_steps):
        data, target = get_batch(train_data, step_num, args.n)
        data = data.to(device)
        target = target.to(device)

        predicted_logits = model(data)
        loss = criterion(predicted_logits, target)

        # reset optimizer state, start loss and move optimizer
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        interval_loss += loss.item()
        if step_num % args.log_interval == 0 and step_num > 0:
            cur_loss = interval_loss / args.log_interval
            elapsed = time.time() - start_time
            ppl = 0
            try:
              ppl = math.exp(cur_loss)
            except OverflowError:
              print("Perplexity too big for log operation, using inf for ppl instead.")
              ppl = float('inf')
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                  'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, step_num,total_steps, args.lr, elapsed * 1000 / args.log_interval, cur_loss, ppl))
            epoch_loss += interval_loss
            interval_loss = 0
            start_time = time.time()
        if args.dry_run:
            break
    return epoch_loss / total_steps

# Loop over epochs.
lr = args.lr
best_val_loss = None

# At any point you can hit Ctrl + C to break out of training early.
try:
    parameters = [param for param in model.parameters() if param.requires_grad]
    optimizer = ""
    if args.optimizer is "sgd":
      optimizer = torch.optim.SGD(parameters, lr=args.lr)
    elif args.optimizer is "rmsprops":
      optimizer = torch.optim.RMSprop(parameters, lr=args.lr)
    else:
      optimizer = torch.optim.Adam(parameters, lr=args.lr)
      
    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train_loss = train(optimizer)

        train_ppl = 0
        try:
          train_ppl = math.exp(train_loss)
        except OverflowError:
          print("using overflowed replacement instead")
          train_ppl = float('inf')

        writer.add_scalar("Loss/train", train_loss, epoch)
        writer.add_scalar("Perplexity/train", train_ppl, epoch)
        writer.add_scalar("EpochTime/train", (time.time() - epoch_start_time), epoch)

        val_loss = evaluate(val_data)
        val_ppl = 0
        try:
          val_ppl = math.exp(val_loss)
        except OverflowError:
          print("using overflowed replacement instead")
          val_ppl = float('inf')
        print('=' * 89)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),val_loss, val_ppl))
        
        writer.add_scalar("Loss/validation", val_loss, epoch)
        writer.add_scalar("Perplexity/validation", val_ppl, epoch)

        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

# Load the best saved model.
with open(args.save, 'rb') as f:
    model = torch.load(f)

# Run on test data.
test_loss = evaluate(test_data)
writer.flush()
ppl = 0
try:
  ppl = math.exp(test_loss)
except OverflowError:
  print("using overflowed replacement instead")
  ppl = float('inf')
print('=' * 89)
print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
    test_loss, ppl))
print('=' * 89)
