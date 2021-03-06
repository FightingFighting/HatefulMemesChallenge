#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""two-stream Transformer encoder."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from functools import partial
from os import name

import numpy as np
from torch import dtype
import paddle
import paddle.fluid as fluid
import paddle.fluid.layers as layers
from loguru import logger


def multi_head_attention(queries,
                         keys,
                         values,
                         attn_bias,
                         d_key,
                         d_value,
                         d_model,
                         n_head=1,
                         dropout_rate=0.,
                         cache=None,
                         param_initializer=None,
                         seed=0,
                         name='multi_head_att',
                         debug=False):
    """
    Multi-Head Attention. Note that attn_bias is added to the logit before
    computing softmax activiation to mask certain selected positions so that
    they will not considered in attention weights.
    """
    # logger.info(f"multi_head_attention seed: {seed}")
    keys = queries if keys is None else keys
    values = keys if values is None else values

    if not (len(queries.shape) == len(keys.shape) == len(values.shape) == 3):
        raise ValueError(
            "Inputs: quries, keys and values should all be 3-D tensors."
            f"len({queries.shape}) == len({keys.shape}) == len({values.shape})")

    def __compute_qkv(queries, keys, values, n_head, d_key, d_value):
        """
        Add linear projection to queries, keys, and values.
        """
        q = layers.fc(input=queries,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(
                          name=name + '_query_fc.w_0',
                          initializer=param_initializer),
                      bias_attr=name + '_query_fc.b_0')
        k = layers.fc(input=keys,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(
                          name=name + '_key_fc.w_0',
                          initializer=param_initializer),
                      bias_attr=name + '_key_fc.b_0')
        v = layers.fc(input=values,
                      size=d_value * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(
                          name=name + '_value_fc.w_0',
                          initializer=param_initializer),
                      bias_attr=name + '_value_fc.b_0')
        return q, k, v

    def __split_heads(x, n_head):
        """
        Reshape the last dimension of inpunt tensor x so that it becomes two
        dimensions and then transpose. Specifically, input a tensor with shape
        [bs, max_sequence_length, n_head * hidden_dim] then output a tensor
        with shape [bs, n_head, max_sequence_length, hidden_dim].
        """
        hidden_size = x.shape[-1]
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        reshaped = layers.reshape(
            x=x, shape=[0, 0, n_head, hidden_size // n_head], inplace=True)

        # permuate the dimensions into:
        # [batch_size, n_head, max_sequence_len, hidden_size_per_head]
        return layers.transpose(x=reshaped, perm=[0, 2, 1, 3])

    def __combine_heads(x):
        """
        Transpose and then reshape the last two dimensions of inpunt tensor x
        so that it becomes one dimension, which is reverse to __split_heads.
        """
        if len(x.shape) == 3: return x
        if len(x.shape) != 4:
            raise ValueError("Input(x) should be a 4-D Tensor.")

        trans_x = layers.transpose(x, perm=[0, 2, 1, 3])
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        return layers.reshape(
            x=trans_x,
            shape=[0, 0, trans_x.shape[2] * trans_x.shape[3]],
            inplace=True)

    def scaled_dot_product_attention(q, k, v, attn_bias, d_key, dropout_rate):
        """
        Scaled Dot-Product Attention
        q: (-1, 16, 80, 64)
        k: (-1, 16, 80, 64)
        v: (-1, 16, 80, 64)
        attn_bias: (-1, 16, 80, 80)
        """
        scaled_q = layers.scale(x=q, scale=d_key ** -0.5)
        product = layers.matmul(x=scaled_q, y=k, transpose_y=True)

        if attn_bias:
            product += attn_bias
        weights = layers.softmax(product)
        if dropout_rate:
            weights = layers.dropout(
                weights,
                dropout_prob=dropout_rate,
                dropout_implementation="upscale_in_train",
                seed=seed,
                is_test=False)
        out = layers.matmul(weights, v)
        # out: (-1, 16, 80, 64)
        return out

    q, k, v = __compute_qkv(queries, keys, values, n_head, d_key, d_value)

    if cache is not None:  # use cache and concat time steps
        # Since the inplace reshape in __split_heads changes the shape of k and
        # v, which is the cache input for next time step, reshape the cache
        # input from the previous time step first.
        k = cache["k"] = layers.concat(
            [layers.reshape(
                cache["k"], shape=[0, 0, d_model]), k], axis=1)
        v = cache["v"] = layers.concat(
            [layers.reshape(
                cache["v"], shape=[0, 0, d_model]), v], axis=1)

    _q = __split_heads(q, n_head)
    _k = __split_heads(k, n_head)
    _v = __split_heads(v, n_head)

    # (-1, 16, 80, 64)
    ctx_multiheads = scaled_dot_product_attention(_q, _k, _v, attn_bias, d_key,
                                                  dropout_rate)

    # (-1 80, 1024)
    out = __combine_heads(ctx_multiheads)

    # Project back to the model size.  (-1 80, 1024)
    proj_out = layers.fc(input=out,
                         size=d_model,
                         num_flatten_dims=2,
                         param_attr=fluid.ParamAttr(
                             name=name + '_output_fc.w_0',
                             initializer=param_initializer),
                         bias_attr=name + '_output_fc.b_0')
    if debug:
        return {
            'proj_out': proj_out,
            # 'ctx_multiheads': ctx_multiheads,
            # 'out': out,
            # 'q': q,
            # 'k': k,
            # 'v': v,
        }
    else:
        return proj_out


def positionwise_feed_forward(x,
                              d_inner_hid,
                              d_hid,
                              dropout_rate,
                              hidden_act,
                              param_initializer=None,
                              seed=0,
                              name='ffn',
                              debug=False):
    """
    Position-wise Feed-Forward Networks.
    This module consists of two linear transformations with a ReLU activation
    in between, which is applied to each position separately and identically.
    """
    # logger.info(f"positionwise_feed_forward seed: {seed}")
    hidden = layers.fc(input=x,
                       size=d_inner_hid,
                       num_flatten_dims=2,
                       act=hidden_act,
                       param_attr=fluid.ParamAttr(
                           name=name + '_fc_0.w_0',
                           initializer=param_initializer),
                       bias_attr=name + '_fc_0.b_0')
    if dropout_rate:
        hidden = layers.dropout(
            hidden,
            dropout_prob=dropout_rate,
            dropout_implementation="upscale_in_train",
            seed=seed,
            is_test=False)
    out = layers.fc(input=hidden,
                    size=d_hid,
                    num_flatten_dims=2,
                    param_attr=fluid.ParamAttr(
                        name=name + '_fc_1.w_0', initializer=param_initializer),
                    bias_attr=name + '_fc_1.b_0')

    if debug:
        return {
            "hidden": hidden,
            "out": out,
        }
    else:
        return out


def pre_post_process_layer(prev_out, out, process_cmd, dropout_rate=0., seed=0,
                           name='', debug=False):
    """
    Add residual connection, layer normalization and droput to the out tensor
    optionally according to the value of process_cmd.
    This will be used before or after multi-head attention and position-wise
    feed-forward networks.
    out: (b, seq-len, hidden size)
    """
    # logger.info(f"pre_post_process_layer out: {prev_out}")
    debug_dict = {}
    for cmd in process_cmd:
        if cmd == "a":  # add residual connection
            out = out + prev_out if prev_out else out
        elif cmd == "n":  # add layer normalization
            out = layers.layer_norm(
                out,
                begin_norm_axis=len(out.shape) - 1,
                param_attr=fluid.ParamAttr(
                    name=name + '_layer_norm_scale',
                    initializer=fluid.initializer.Constant(1.)),
                bias_attr=fluid.ParamAttr(
                    name=name + '_layer_norm_bias',
                    initializer=fluid.initializer.Constant(0.)))
        elif cmd == "d":  # add dropout
            if dropout_rate:
                out = layers.dropout(
                    out,
                    dropout_prob=dropout_rate,
                    dropout_implementation="upscale_in_train",
                    seed=seed,
                    is_test=False)
        
        if debug:
            debug_dict[f"out_{cmd}"] = out
    if debug:
        debug_dict[f"out"] = out
        return debug_dict
    else:
        return out


pre_process_layer = partial(pre_post_process_layer, None)
post_process_layer = pre_post_process_layer


def encoder_co_layer(enc_input,
                     enc_vl_input,
                     attn_vl_bias,
                     co_head,
                     co_key,
                     co_value,
                     co_model,
                     d_model,
                     d_inner_hid,
                     v_model,
                     v_inner_hid,
                     prepostprocess_dropout,
                     attention_dropout,
                     relu_dropout,
                     hidden_act,
                     preprocess_cmd="n",
                     postprocess_cmd="da",
                     param_initializer=None,
                     seed=0,
                     name='',
                     debug=False):
    """
    Co_layer to perform co-attention from visual to language or from language to visual 
    enc_input: (-1, 80, 1024)
    enc_vl_input: (-1, 100, 1024)
    attn_vl_bias: (-1, 16, 100, 80)
    
    co_head -> int: 16 
    co_value -> int: 64
    co_model -> int: 1024
    d_model -> int: 1024
    d_inner_hid -> 4096
    v_model -> int: 1024
    v_inner_hid -> int: 4096

    relu_dropout -> float: 0
    hidden_act -> str: 'gelu'
    """
    enc_input_pre = pre_process_layer(
                            enc_input,
                            preprocess_cmd,
                            prepostprocess_dropout,
                            name=name + '_pre_att',
                            seed=seed)

    enc_input_vl_pre = pre_process_layer(
                            enc_vl_input,
                            preprocess_cmd,
                            prepostprocess_dropout,
                            name=name + '_vl_pre_att',
                            seed=seed)

    attn_output = multi_head_attention(
        enc_input_pre,
        enc_input_vl_pre,
        enc_input_vl_pre,
        layers.transpose(attn_vl_bias, perm=[0, 1, 3, 2]),
        co_key,
        co_value,
        d_model,
        co_head,
        attention_dropout,
        param_initializer=param_initializer,
        name=name + '_multi_head_att',
        seed=seed)

    attn_vl_output = multi_head_attention(
        enc_input_vl_pre,
        enc_input_pre,
        enc_input_pre,
        attn_vl_bias,
        co_key,
        co_value,
        v_model,
        co_head,
        attention_dropout,
        param_initializer=param_initializer,
        name=name + '_vl_multi_head_att',
        seed=seed)

    attn_output = post_process_layer(
        enc_input,
        attn_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_post_att',
        seed=seed)

    attn_vl_output = post_process_layer(
        enc_vl_input,
        attn_vl_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_vl_post_att',
        seed=seed,)

    ffd_output = positionwise_feed_forward(
        pre_process_layer(
            attn_output,
            preprocess_cmd,
            prepostprocess_dropout,
            name=name + '_pre_ffn',
            seed=seed),
        d_inner_hid,
        d_model,
        relu_dropout,
        hidden_act,
        param_initializer=param_initializer,
        name=name + '_ffn',
        seed=seed)

    ffd_vl_output = positionwise_feed_forward(
        pre_process_layer(
            attn_vl_output,
            preprocess_cmd,
            prepostprocess_dropout,
            name=name + '_pre_vl_ffn',
            seed=seed),
        v_inner_hid,
        v_model,
        relu_dropout,
        hidden_act,
        param_initializer=param_initializer,
        name=name + '_vl_ffn',
        seed=seed)

    enc_output = post_process_layer(
        attn_output,
        ffd_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_post_ffn',
        seed=seed)

    enc_vl_output = post_process_layer(
        attn_vl_output,
        ffd_vl_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_vl_post_ffn',
        seed=seed)

    if debug:
        return {
            'enc_output': enc_output,
            'enc_vl_output': enc_vl_output,
        }
    else:
        return enc_output, enc_vl_output


def encoder_layer(enc_input,
                  attn_bias,
                  n_head,
                  d_key,
                  d_value,
                  d_model,
                  d_inner_hid,
                  prepostprocess_dropout,
                  attention_dropout,
                  relu_dropout,
                  hidden_act,
                  preprocess_cmd="n",
                  postprocess_cmd="da",
                  param_initializer=None,
                  seed=0,
                  name='',
                  debug=False):
    """The encoder layers that can be stacked to form a deep encoder.
    This module consits of a multi-head (self) attention followed by
    position-wise feed-forward networks and both the two components companied
    with the post_process_layer to add residual connection, layer normalization
    and droput.
    """
    attn_output = multi_head_attention(
        pre_process_layer(
            enc_input,
            preprocess_cmd,
            prepostprocess_dropout,
            name=name + '_pre_att',
            seed=seed),
        None,
        None,
        attn_bias,
        d_key,
        d_value,
        d_model,
        n_head,
        attention_dropout,
        param_initializer=param_initializer,
        name=name + '_multi_head_att',
        seed=seed)
    attn_output = post_process_layer(
        enc_input,
        attn_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_post_att',
        seed=seed)
    ffd_output = positionwise_feed_forward(
        pre_process_layer(
            attn_output,
            preprocess_cmd,
            prepostprocess_dropout,
            name=name + '_pre_ffn',
            seed=seed),
        d_inner_hid,
        d_model,
        relu_dropout,
        hidden_act,
        param_initializer=param_initializer,
        name=name + '_ffn',
        seed=seed)
    
    post_ffd = post_process_layer(
        attn_output,
        ffd_output,
        postprocess_cmd,
        prepostprocess_dropout,
        name=name + '_post_ffn',
        seed=seed)

    if debug:
        return {
            'attn_output': attn_output,
            'post_ffd': post_ffd
        }
    else:
        return post_ffd


def encoder(enc_input,
            enc_vl_input,
            attn_bias,
            attn_image_bias,
            attn_vl_bias,
            n_layer,
            n_head,
            d_key,
            d_value,
            d_model,
            d_inner_hid,
            v_head,
            v_key,
            v_value,
            v_model,
            v_inner_hid,
            co_head,
            co_key,
            co_value,
            co_model,
            co_inner_hid,
            prepostprocess_dropout,
            attention_dropout,
            relu_dropout,
            hidden_act,
            preprocess_cmd="n",
            postprocess_cmd="da",
            param_initializer=None,
            v_biattention_id=[0, 1, 2, 3, 4, 5],
            t_biattention_id=[18, 19, 20, 21, 22, 23],
            seed=0,
            name='',
            debug=False):
    """
    The encoder is composed of a stack of identical layers returned by calling
    encoder_layer and encoder_co_layer
    enc_input: (-1, 80, 1024),
    enc_vl_input: (-1, 100, 1024),
    attn_bias: (-1, 16, 80, 80),
    attn_image_bias: (-1, 16, 100, 100)
    attn_vl_bias: (-1, 16, 100, 80)
    n_layer: int -> 24
    n_head : int -> 16
    """

    v_start = 0
    t_start = 0
    block = 0

    for v_layer_id, t_layer_id in zip(v_biattention_id, t_biattention_id):
        v_end = v_layer_id
        t_end = t_layer_id
        for idx in range(t_start, t_end):
            enc_output = encoder_layer(
                 enc_input,
                 attn_bias,
                 n_head,
                 d_key,
                 d_value,
                 d_model,
                 d_inner_hid,
                 prepostprocess_dropout,
                 attention_dropout,
                 relu_dropout,
                 hidden_act,
                 preprocess_cmd,
                 postprocess_cmd,
                 param_initializer=param_initializer,
                 name=name + '_layer_' + str(idx),
                 seed=seed)
            enc_input = enc_output

        for idx in range(v_start, v_end):
            enc_vl_output = encoder_layer(
                 enc_vl_input,
                 attn_image_bias,
                 v_head,
                 v_key,
                 v_value,
                 v_model,
                 v_inner_hid,
                 prepostprocess_dropout,
                 attention_dropout,
                 relu_dropout,
                 hidden_act,
                 preprocess_cmd,
                 postprocess_cmd,
                 param_initializer=param_initializer,
                 name=name + '_vlayer_' + str(idx),
                 seed=seed)
            enc_vl_input = enc_vl_output

        enc_output, enc_vl_output = encoder_co_layer(
             enc_input,
             enc_vl_input,
             attn_vl_bias,
             co_head,
             co_key,
             co_value,
             co_model,
             d_model,
             d_inner_hid,
             v_model,
             v_inner_hid,
             prepostprocess_dropout,
             attention_dropout,
             relu_dropout,
             hidden_act,
             preprocess_cmd,
             postprocess_cmd,
             param_initializer=param_initializer,
             name=name + '_colayer_' + str(block),
             seed=seed)

        enc_input, enc_vl_input = enc_output, enc_vl_output
        
        block += 1
        v_start = v_end
        t_start = t_end

    enc_output = encoder_layer(
         enc_output,
         attn_bias,
         n_head,
         d_key,
         d_value,
         d_model,
         d_inner_hid,
         prepostprocess_dropout,
         attention_dropout,
         relu_dropout,
         hidden_act,
         preprocess_cmd,
         postprocess_cmd,
         param_initializer=param_initializer,
         name=name + '_layer_' + str(t_end),
         seed=seed)

    enc_vl_output = encoder_layer(
         enc_vl_output,
         attn_image_bias,
         v_head,
         v_key,
         v_value,
         v_model,
         v_inner_hid,
         prepostprocess_dropout,
         attention_dropout,
         relu_dropout,
         hidden_act,
         preprocess_cmd,
         postprocess_cmd,
         param_initializer=param_initializer,
         name=name + '_vlayer_' + str(v_end),
         seed=seed)

    enc_output = pre_process_layer(
        enc_output, preprocess_cmd, prepostprocess_dropout, name="post_encoder", seed=seed)

    enc_vl_output = pre_process_layer(
        enc_vl_output, preprocess_cmd, prepostprocess_dropout, name="vl_post_encoder", seed=seed)
    
    if debug:
        return {
            'enc_output': enc_output,
            'enc_vl_output': enc_vl_output
        }
    else:
        return enc_output, enc_vl_output


def init_pretraining_params(exe, pretraining_params_path, main_program):
    """
    init pretraining params without lr and step info
    """
    assert os.path.exists(pretraining_params_path
                          ), "[%s] cann't be found." % pretraining_params_path

    def existed_params(var):
        """
        Check existed params
        """
        if not isinstance(var, paddle.fluid.framework.Parameter):
            print('dont exitsted: ', var.name)
            return False
        print('exitsted: ', var.name)
        return os.path.exists(os.path.join(pretraining_params_path, var.name))

    paddle.fluid.io.load_vars(
        exe,
        pretraining_params_path,
        main_program=main_program,
        predicate=existed_params)
    print("Load pretraining parameters from {}.".format(
        pretraining_params_path))


def test_multi_head_attention():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            img_seq_len = 40

            dA = layers.data('A', shape=[seq_len, hidden_size])
            dB = layers.data('B', shape=[img_seq_len, hidden_size])
            dBias = layers.data('dBias', shape=[16, seq_len, img_seq_len])
            
            out_dict = multi_head_attention(
                dA, dB, dB, dBias,
                64, 64, hidden_size,
                n_head=16, dropout_rate=0.0,
                name='encoder_vlayer_3_multi_head_att',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    B = np.random.normal(size=[2, img_seq_len, hidden_size]).astype(np.float32)
    bias = np.ones([2, 16, seq_len, img_seq_len], dtype=np.float32)
    feed = {
        'A': A,
        'B': B,
        'dBias': bias,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


def test_positionwise_feed_forward():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            
            dA = layers.data('A', shape=[seq_len, hidden_size])
            
            out_dict = positionwise_feed_forward(
                dA, 4096, hidden_size, 0, 'gelu',
                name='encoder_vlayer_5_ffn',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    feed = {
        'A': A,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


def test_pre_post_process_layer():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            
            dA = layers.data('A', shape=[seq_len, hidden_size])
            attn_output = layers.data('B', shape=[seq_len, hidden_size])
            
            out_dict = post_process_layer(
                dA,
                attn_output,
                "dan",
                0.0,
                name='encoder_layer_9_post_att',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    B = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    feed = {
        'A': A,
        'B': B,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


def test_encoder_co_layer():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            img_seq_len = 40
            n_head = 16

            dA = layers.data('A', shape=[seq_len, hidden_size])
            dB = layers.data('B', shape=[img_seq_len, hidden_size])
            dBias = layers.data('dBias', shape=[16, img_seq_len, seq_len])
            
            out_dict = encoder_co_layer(
                dA, dB, dBias,
                n_head, 64, 64, 1024,
                1024, 4096,
                1024, 4096,
                0, 0, 0,
                'gelu',
                preprocess_cmd="",
                postprocess_cmd="dan",
                name='encoder_colayer_2',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    B = np.random.normal(size=[2, img_seq_len, hidden_size]).astype(np.float32)
    bias = np.random.normal(size=[2, 16, img_seq_len, seq_len]).astype(np.float32)
    feed = {
        'A': A,
        'B': B,
        'dBias': bias,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


def test_encoder_layer():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            img_seq_len = 40
            n_head = 16

            dA = layers.data('A', shape=[seq_len, hidden_size])
            dBias = layers.data('dBias', shape=[16, seq_len, seq_len])
            
            out_dict = encoder_layer(
                dA, dBias,
                n_head, 64, 64, 1024, 4096,
                0, 0, 0,
                'gelu',
                preprocess_cmd="",
                postprocess_cmd="dan",
                name='encoder_layer_4',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    bias = np.random.normal(size=[2, 16, seq_len, seq_len]).astype(np.float32)
    feed = {
        'A': A,
        'dBias': bias,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


def test_encoder():
    startup_prog = fluid.Program()
    test_prog = fluid.Program()
    
    with fluid.program_guard(test_prog, startup_prog):
        with fluid.unique_name.guard():
            hidden_size = 1024
            seq_len = 80
            img_seq_len = 40
            n_head = 16
            n_layer = 24

            dA = layers.data('A', shape=[seq_len, hidden_size])
            dB = layers.data('B', shape=[img_seq_len, hidden_size])
            aBias = layers.data('aBias', shape=[16, seq_len, seq_len])
            bBias = layers.data('bBias', shape=[16, img_seq_len, img_seq_len])
            abBias = layers.data('abBias', shape=[16, img_seq_len, seq_len])
            
            out_dict = encoder(
                dA, dB, aBias, bBias, abBias,
                n_layer,
                n_head, 64, 64, 1024, 4096,
                n_head, 64, 64, 1024, 4096,
                n_head, 64, 64, 1024, 4096,
                0, 0, 0,
                'gelu',
                preprocess_cmd="",
                postprocess_cmd="dan",
                v_biattention_id=[0, 1, 2, 3, 4, 5],
                t_biattention_id=[18, 19, 20, 21, 22, 23],
                name='encoder',
                debug=True
            )

    place = fluid.CUDAPlace(0)
    exe = fluid.Executor(place)
    init_pretraining_params(
        exe,
        '/home/ron_zhu/Disk2/ernie/model-ernie-vil-large-en/params',
        test_prog
    )
    
    np.random.seed(1234)
    A = np.random.normal(size=[2, seq_len, hidden_size]).astype(np.float32)
    B = np.random.normal(size=[2, img_seq_len, hidden_size]).astype(np.float32)
    # bias = np.random.normal(size=[2, 16, img_seq_len, seq_len]).astype(np.float32)
    a_bias = np.random.normal(size=[2, n_head, seq_len, seq_len]).astype(np.float32)
    b_bias = np.random.normal(size=[2, n_head, img_seq_len, img_seq_len]).astype(np.float32)
    ab_bias = np.random.normal(size=[2, n_head, img_seq_len, seq_len]).astype(np.float32)
    
    feed = {
        'A': A,
        'B': B,
        'aBias': a_bias,
        'bBias': b_bias,
        'abBias': ab_bias,
    }
    output = exe.run(test_prog, feed=feed, fetch_list=list(out_dict.values()))
    name_to_val = {k: v for k, v in zip(out_dict.keys(), output)}
    for k, v in name_to_val.items():
        print(k)
        print(v.shape)
        print(v)
        print(v[0][0])
        print('-' * 100)
    
    param_list = exe.run(fetch_list=test_prog.all_parameters())
    param_names = [p.name for p in test_prog.all_parameters()]
    import pdb; pdb.set_trace()


if __name__ == "__main__":
    # test_multi_head_attention()
    # test_positionwise_feed_forward()
    # test_pre_post_process_layer()
    # test_encoder_co_layer()
    # test_encoder_layer()
    test_encoder()