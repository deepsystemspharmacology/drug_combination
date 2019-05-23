import torch.nn as nn
from torch import cat
import torch
from Layers import EncoderLayer, DecoderLayer, OutputAttentionLayer
from Sublayers import Norm, OutputFeedForward
import copy
import setting
from attention_main import use_cuda, device2


def get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


class Encoder(nn.Module):
    def __init__(self, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.layers = get_clones(EncoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)

    def forward(self, src, mask=None):
        x = src
        for i in range(self.N):
            x = self.layers[i](x, mask)
        return self.norm(x)


class Decoder(nn.Module):
    def __init__(self, d_model, N, heads, dropout):
        super().__init__()
        self.N = N
        self.layers = get_clones(DecoderLayer(d_model, heads, dropout), N)
        self.norm = Norm(d_model)

    def forward(self, trg, e_outputs, src_mask=None, trg_mask=None):
        x = trg
        for i in range(self.N):
            x = self.layers[i](x, e_outputs, src_mask, trg_mask)
        return self.norm(x)


class Transformer(nn.Module):
    def __init__(self, d_model, N, heads, dropout):
        super().__init__()
        self.encoder = Encoder(d_model, N, heads, dropout)
        self.decoder = Decoder(d_model, N, heads, dropout)

    def forward(self, src, trg, src_mask=None, trg_mask=None):
        e_outputs = self.encoder(src, src_mask)
        # print("DECODER")
        d_output = self.decoder(trg, e_outputs, src_mask, trg_mask)
        flat_d_output = d_output.view(-1, d_output.size(-2)*d_output.size(-1))
        return flat_d_output

class TransformerPlusLinear(Transformer):
    def __init__(self, d_input, d_model, n_feature_type, N, heads, dropout):
        super().__init__(d_model, N, heads, dropout)
        self.input_linear = nn.Linear(d_input, d_model)
        self.out = OutputFeedForward(d_model, n_feature_type, d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src, trg, src_mask=None, trg_mask=None):
        src = self.input_linear(src)
        trg = self.input_linear(trg)
        flat_d_output = super().forward(src, trg)
        output = self.out(flat_d_output)
        return output

class FlexibleTransformer(Transformer):

    def __init__(self, inputs_lengths, d_model, n_feature_type_list, N, heads, dropout):
        super().__init__(d_model, N, heads, dropout)
        self.final_inputs = nn.ModuleList()
        self.linear_layers = nn.ModuleList()
        for i in range(len(inputs_lengths)):
            self.linear_layers.append(nn.Linear(inputs_lengths[i], d_model))
        out_input_length = d_model * sum(n_feature_type_list)
        self.out = OutputFeedForward(out_input_length, 1, d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):

        assert len(self.linear_layers) == len(src_list), "Features and sources length are different"
        final_srcs = []
        final_trgs = []
        for i in range(len(self.linear_layers)):
            final_srcs.append(self.linear_layers[i](src_list[i]))
            final_trgs.append(self.linear_layers[i](trg_list[i]))
        final_src = cat(tuple(final_srcs), 1)
        final_trg = cat(tuple(final_trgs), 1)
        flat_d_output = super().forward(final_src, final_trg)
        output = self.out(flat_d_output)
        return output

class MultiTransformers(nn.Module):

    def __init__(self, d_input_list, d_model_list, n_feature_type_list, N, heads, dropout):
        super().__init__()

        assert len(d_input_list) == len(n_feature_type_list) and len(d_input_list) == len(d_model_list),\
            "claimed inconsistent number of transformers"
        self.linear_layers = nn.ModuleList()
        for i in range(len(d_input_list)):
            self.linear_layers.append(nn.Linear(d_input_list[i], d_model_list[i]))
        self.transformer_list = nn.ModuleList()
        self.n_feature_type_list = n_feature_type_list
        for i in range(len(d_input_list)):
            self.transformer_list.append(Transformer(d_model_list[i], N, heads, dropout))

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):

        assert len(src_list) == len(self.transformer_list), "inputs length is not same with input length for model"
        src_list_linear = []
        trg_list_linear = []
        for i in range(len(self.linear_layers)):
            src_list_linear.append(self.linear_layers[i](src_list[i]))
            trg_list_linear.append(self.linear_layers[i](trg_list[i]))
        output_list = []
        for i in range(len(self.transformer_list)):
            output_list.append(self.transformer_list[i](src_list_linear[i], trg_list_linear[i]))

        return output_list

class MultiTransformersPlusLinear(MultiTransformers):

    def __init__(self, d_input_list, d_model_list, n_feature_type_list, N, heads, dropout):

        super().__init__(d_input_list, d_model_list, n_feature_type_list, N, heads, dropout)
        out_input_length = sum([d_model_list[i] * n_feature_type_list[i] for i in range(len(d_model_list))])
        self.out = OutputFeedForward(out_input_length, 1, d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):

        output_list = super().forward(src_list, trg_list)
        cat_output = cat(tuple(output_list), dim=1)
        output = self.out(cat_output)
        return output

class MultiTransformersPlusSDPAttention(MultiTransformers):

    def __init__(self, d_input_list, d_model_list, n_feature_type_list, N, heads, dropout):

        super().__init__(d_input_list, d_model_list, n_feature_type_list, N, heads, dropout)
        self.n_feature_type_list = n_feature_type_list
        out_input_length = sum([d_model_list[i] * n_feature_type_list[i] for i in range(len(d_model_list)-1)])
        self.output_attn = OutputAttentionLayer(d_model_list[0], d_model_list[-1])
        H = sum(self.n_feature_type_list)-1
        self.out = OutputFeedForward(1, d_model_list[-1], d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):
        output_list = super().forward(src_list, trg_list)
        bs = output_list[0].size(0)
        for i, output_tensor in enumerate(output_list):
            output_list[i] = output_tensor.contiguous().view(bs, self.n_feature_type_list[i], -1)
        cat_output = cat(tuple(output_list[:-1]), dim=1)
        attn_output = self.output_attn(output_list[-1], cat_output)
        attn_output = attn_output.contiguous().view(bs, -1)
        output = self.out(attn_output)
        return output

class MultiTransformersPlusRNN(MultiTransformers):

    def __init__(self, d_input_list, d_model_list, n_feature_type_list, N, heads, dropout):

        super().__init__(d_input_list, d_model_list, n_feature_type_list, N, heads, dropout)
        self.n_feature_type_list = n_feature_type_list
        out_input_length = sum([d_model_list[i] * n_feature_type_list[i] for i in range(len(d_model_list)-1)])
        self.hidden_size = 200
        self.rnn = nn.LSTM(input_size=d_model_list[0], hidden_size=self.hidden_size, num_layers=1, batch_first=True)
        self.out = OutputFeedForward(sum(self.n_feature_type_list), self.hidden_size, d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):
        output_list = super().forward(src_list, trg_list)
        bs = output_list[0].size(0)
        for i, output_tensor in enumerate(output_list):
            output_list[i] = output_tensor.contiguous().view(bs, self.n_feature_type_list[i], -1)
        cat_output = cat(tuple(output_list), dim=1)
        h_s, c_s = torch.randn(1, bs, self.hidden_size), torch.randn(1, bs, self.hidden_size)
        if use_cuda:
            h_s = h_s.to(device2)
            c_s = c_s.to(device2)
        rnn_output, hidden = self.rnn(cat_output, (h_s, c_s))
        attn_output = rnn_output.contiguous().view(bs, -1)
        output = self.out(attn_output)
        return output

class MultiTransformersPlusMulAttention(MultiTransformers):

    def __init__(self, d_input_list, d_model_list, n_feature_type_list, N, heads, dropout):

        super().__init__(d_input_list, d_model_list, n_feature_type_list, N, heads, dropout)
        out_input_length = sum([d_model_list[i] * n_feature_type_list[i] for i in range(len(d_model_list)-1)])
        self.hidden_size = 20
        self.linear = nn.Linear(d_model_list[-1], self.hidden_size)
        H = sum(self.n_feature_type_list)-1
        self.out = OutputFeedForward(H*self.hidden_size, d_model_list[-1],d_layers=setting.output_FF_layers, dropout=dropout)

    def forward(self, src_list, trg_list, src_mask=None, trg_mask=None):
        output_list = super().forward(src_list, trg_list)
        bs = output_list[0].size(0)
        for i, output_tensor in enumerate(output_list):
            output_list[i] = output_tensor.contiguous().view(bs, self.n_feature_type_list[i], -1)
        cat_output = cat(tuple(output_list[:-1]), dim=1)
        cat_output = self.linear(cat_output)
        mul_output = torch.matmul(cat_output.contiguous().view(bs,-1,1), output_list[-1].contiguous().view(bs,1,-1))
        output = self.out(mul_output.contiguous().view(bs,-1))
        return output

def get_model(inputs_lengths):
    assert setting.d_model % setting.attention_heads == 0
    assert setting.attention_dropout < 1

    #model = TransformerPlusLinear(setting.d_input, setting.d_model, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    model = FlexibleTransformer(inputs_lengths, setting.d_model, setting.n_layers, setting.attention_heads, setting.attention_dropout)

    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)

    return model

def get_multi_models(inputs_lengths):

    if not isinstance(setting.d_model, list):
        d_models = [setting.d_model] * len(inputs_lengths)
    else:
        d_models = setting.d_model

    if not isinstance(setting.n_feature_type, list):
        n_feature_types = [setting.n_feature_type] * len(inputs_lengths)
    else:
        n_feature_types = setting.n_feature_type

    for d_model in d_models:
        assert d_model % setting.attention_heads == 0
    assert setting.attention_dropout < 1

    final_inputs_lengths = [inputs_lengths[i]//n_feature_types[i] for i in range(len(inputs_lengths))]
    #model = FlexibleTransformer(final_inputs_lengths, setting.d_model, setting.n_feature_type, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    #model = TransformerPlusLinear(final_inputs_lengths, d_models, setting.n_feature_type, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    #model = MultiTransformersPlusLinear(final_inputs_lengths, final_inputs_lengths, n_feature_types, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    #model = MultiTransformersPlusLinear(final_inputs_lengths, d_models, n_feature_types, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    #model = MultiTransformersPlusSDPAttention(final_inputs_lengths, d_models, n_feature_types, setting.n_layers, setting.attention_heads, setting.attention_dropout)
    model = MultiTransformersPlusMulAttention(final_inputs_lengths, d_models, n_feature_types, setting.n_layers, setting.attention_heads, setting.attention_dropout)

    for p in model.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)

    return model