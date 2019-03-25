'''
 @Date  : 2017/12/18
 @Author: Shuming Ma
 @mail  : shumingma@pku.edu.cn 
 @homepage: shumingma.com
'''

import torch
import torch.nn as nn
from torch.autograd import Variable
import utils
import models


class seq2seq(nn.Module):

    def __init__(self, config, use_attention=True, encoder=None, decoder=None):
        super(seq2seq, self).__init__()

        if encoder is not None:
            self.encoder = encoder
        else:
            self.encoder = models.rnn_encoder(config)
        tgt_embedding = self.encoder.embedding if config.shared_vocab else None
        if decoder is not None:
            self.decoder = decoder
        else:
            self.decoder = models.rnn_decoder(config, embedding=tgt_embedding, use_attention=use_attention)
        self.log_softmax = nn.LogSoftmax(dim=-1)
        self.use_cuda = config.use_cuda
        self.config = config
        self.criterion = nn.CrossEntropyLoss(ignore_index=utils.PAD, size_average=False)
        if config.use_cuda:
            self.criterion.cuda()

    def one_hot(self, seq_batch, depth):
        # seq_batch.size() should be [seq,batch] or [batch,]
        # return size() would be [seq,batch,depth] or [batch,depth]
        out = torch.zeros(seq_batch.size() + torch.Size([depth]))
        dim = len(seq_batch.size())
        index = seq_batch.view(seq_batch.size() + torch.Size([1]))
        return out.scatter_(dim, index, 1)

    def compute_loss(self, scores, targets):
        if self.config.max_split > 0:
            outputs = Variable(scores.data, requires_grad=True, volatile=False)
            loss = 0
            outputs_split = torch.split(outputs, self.config.max_split)
            targets_split = torch.split(targets.contiguous(), self.config.max_split)
            for i, (out_t, targ_t) in enumerate(zip(outputs_split, targets_split)):
                out_t = out_t.view(-1, out_t.size(2))
                loss_t = self.criterion(out_t, targ_t.view(-1)).sum()
                num_total_t = targ_t.ne(utils.PAD).data.sum()
                loss += loss_t.data[0]
                loss_t.div(num_total_t).backward()
            grad_output = outputs.grad.data
            scores.backward(grad_output)
        else:
            scores = scores.view(-1, scores.size(2))
            loss = self.criterion(scores, targets.contiguous().view(-1))
        return loss

    def forward(self, src, src_len, dec, targets):
        src = src.t()
        dec = dec.t()
        targets = targets.t()

        contexts, state = self.encoder(src, src_len.data.tolist())
        if self.decoder.attention is not None:
            self.decoder.attention.init_context(context=contexts)
        outputs = []
        for input in dec.split(1):
            output, state, attn_weights = self.decoder(input.squeeze(0), state)
            outputs.append(output)
        outputs = torch.stack(outputs)

        loss = self.compute_loss(outputs, targets)
        return loss, outputs

    def sample(self, src, src_len):

        lengths, indices = torch.sort(src_len, dim=0, descending=True)
        _, reverse_indices = torch.sort(indices)
        src = torch.index_select(src, dim=0, index=indices)
        bos = Variable(torch.ones(src.size(0)).long().fill_(utils.BOS), volatile=True)
        src = src.t()

        if self.use_cuda:
            bos = bos.cuda()

        contexts, state = self.encoder(src, lengths.data.tolist())
        if self.decoder.attention is not None:
            self.decoder.attention.init_context(context=contexts)
        inputs, outputs, attn_matrix = [bos], [], []
        for i in range(self.config.max_time_step):
            output, state, attn_weights = self.decoder(inputs[i], state)
            predicted = output.max(1)[1]
            #predicted = torch.multinomial(torch.nn.functional.softmax(output/0.6), num_samples=1).squeeze(1)
            inputs += [predicted]
            outputs += [predicted]
            attn_matrix += [attn_weights]

        outputs = torch.stack(outputs)
        sample_ids = torch.index_select(outputs, dim=1, index=reverse_indices).t().data

        if self.decoder.attention is not None:
            attn_matrix = torch.stack(attn_matrix)
            alignments = attn_matrix.max(2)[1]
            alignments = torch.index_select(alignments, dim=1, index=reverse_indices).t().data
        else:
            alignments = None

        return sample_ids, alignments

    def beam_sample(self, src, src_len, beam_size=1):

        # (1) Run the encoder on the src.

        lengths, indices = torch.sort(src_len, dim=0, descending=True)
        _, ind = torch.sort(indices)
        src = torch.index_select(src, dim=0, index=indices)
        src = src.t()
        batch_size = src.size(1)
        contexts, encState = self.encoder(src, lengths.data.tolist())

        #  (1b) Initialize for the decoder.
        def var(a):
            return Variable(a, volatile=True)

        def rvar(a):
            return var(a.repeat(1, beam_size, 1))

        def bottle(m):
            return m.view(batch_size * beam_size, -1)

        def unbottle(m):
            return m.view(beam_size, batch_size, -1)

        # Repeat everything beam_size times.
        contexts = rvar(contexts.data)

        if self.config.cell == 'lstm':
            decState = (rvar(encState[0].data), rvar(encState[1].data))
        else:
            decState = rvar(encState.data)
        #decState.repeat_beam_size_times(beam_size)
        beam = [models.Beam(beam_size, n_best=1,
                          cuda=self.use_cuda, length_norm=self.config.length_norm)
                for __ in range(batch_size)]
        if self.decoder.attention is not None:
            self.decoder.attention.init_context(contexts)

        # (2) run the decoder to generate sentences, using beam search.

        for i in range(self.config.max_time_step):

            if all((b.done() for b in beam)):
                break

            # Construct batch x beam_size nxt words.
            # Get all the pending current beam words and arrange for forward.
            inp = var(torch.stack([b.getCurrentState() for b in beam])
                      .t().contiguous().view(-1))

            # Run one step.
            output, decState, attn = self.decoder(inp, decState)
            # decOut: beam x rnn_size

            # (b) Compute a vector of batch*beam word scores.
            output = unbottle(self.log_softmax(output))
            attn = unbottle(attn)
                # beam x tgt_vocab

            # (c) Advance each beam.
            # update state
            for j, b in enumerate(beam):
                b.advance(output.data[:, j], attn.data[:, j])
                b.beam_update(decState, j)

        # (3) Package everything up.
        allHyps, allScores, allAttn = [], [], []

        for j in ind.data:
            b = beam[j]
            n_best = 1
            scores, ks = b.sortFinished(minimum=n_best)
            hyps, attn = [], []
            for i, (times, k) in enumerate(ks[:n_best]):
                hyp, att = b.getHyp(times, k)
                hyps.append(hyp)
                attn.append(att.max(1)[1])
            allHyps.append(hyps[0])
            allScores.append(scores[0])
            allAttn.append(attn[0])

        return allHyps, allAttn