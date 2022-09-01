# copyright (c) 2021 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Code based on: 
# https://github.com/deepinsight/insightface/blob/master/recognition/arcface_torch/backbones/vit.py

from collections.abc import Callable
import collections

import os
import math
import warnings
import numpy as np
import paddle
import paddle.nn as nn
from paddle.nn.initializer import Constant, Normal, XavierUniform

from plsc.utils import logger
from .layers import PartialFC
from plsc.models.layers import Model

__all__ = [
    'FaceViT_tiny_patch9_112', 'FaceViT_small_patch9_112',
    'FaceViT_base_patch9_112', 'FaceViT_large_patch9_112', 'FaceViT'
]

mlp_bias_normal_ = Normal(std=1e-6)
xavier_uniform_ = XavierUniform()
zeros_ = Constant(value=0.)
minus_tens_ = Constant(value=-10.)
ones_ = Constant(value=1.)


@paddle.no_grad()
def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # Modified based on PyTorch nn.init.trunc_normal_
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn(
            "mean is more than 2 std from [a, b] in trunc_normal_. "
            "The distribution of values may be incorrect.",
            stacklevel=2)

    # Values are generated by using a truncated uniform distribution and
    # then using the inverse CDF for the normal distribution.
    # Get upper and lower cdf values
    l = norm_cdf((a - mean) / std)
    u = norm_cdf((b - mean) / std)

    # Uniformly fill tensor with values from [l, u], then translate to
    # [2l-1, 2u-1].
    tmp = paddle.zeros_like(tensor, dtype='float32')
    tmp.uniform_(2 * l - 1, 2 * u - 1)

    # Use inverse cdf transform for normal distribution to get truncated
    # standard normal
    tmp.erfinv_()

    # Transform to proper mean, std
    tmp.scale_(std * math.sqrt(2.))
    tmp.add_(paddle.to_tensor(mean, dtype='float32'))

    # Clip to ensure it's in the proper range
    tmp.clip_(min=a, max=b)
    tmp = tmp.astype(tensor.dtype)
    tensor.copy_(tmp, False)
    return tensor


def to_2tuple(x):
    return tuple([x] * 2)


def drop_path(x, drop_prob=0., training=False):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).
    the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ...
    """
    if drop_prob == 0. or not training:
        return x
    keep_prob = paddle.to_tensor(1 - drop_prob)
    shape = (paddle.shape(x)[0], ) + (1, ) * (x.ndim - 1)
    if x.dtype == paddle.float16:
        random_tensor = keep_prob + paddle.rand(
            shape, dtype=paddle.float32).astype(x.dtype)
    else:
        random_tensor = keep_prob + paddle.rand(shape, dtype=x.dtype)
    random_tensor = paddle.floor(random_tensor)  # binarize
    output = x.divide(keep_prob) * random_tensor
    return output


class DropPath(nn.Layer):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """

    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class Identity(nn.Layer):
    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, input):
        return input


class Mlp(nn.Layer):
    def __init__(self,
                 in_features,
                 hidden_features=None,
                 out_features=None,
                 act_layer=nn.ReLU6,
                 drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            xavier_uniform_(m.weight)
            mlp_bias_normal_(m.bias)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Layer):
    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.qkv = nn.Linear(dim, dim * 3, bias_attr=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                zeros_(m.bias)

    def forward(self, x):
        # B= paddle.shape(x)[0]
        N, C = x.shape[1:]
        qkv = self.qkv(x).reshape((-1, N, 3, self.num_heads, C //
                                   self.num_heads)).transpose((2, 0, 3, 1, 4))
        q, k, v = qkv[0], qkv[1], qkv[2]

        attn = (q.matmul(k.transpose((0, 1, 3, 2)))) * self.scale
        attn = nn.functional.softmax(attn, axis=-1)
        attn = self.attn_drop(attn)

        x = (attn.matmul(v)).transpose((0, 2, 1, 3)).reshape((-1, N, C))
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Layer):
    def __init__(self,
                 dim,
                 num_heads,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 act_layer=nn.ReLU6,
                 norm_layer='nn.LayerNorm',
                 epsilon=1e-5):
        super().__init__()
        if isinstance(norm_layer, str):
            self.norm1 = eval(norm_layer)(dim, epsilon=epsilon)
        elif isinstance(norm_layer, Callable):
            self.norm1 = norm_layer(dim)
        else:
            raise TypeError(
                "The norm_layer must be str or paddle.nn.layer.Layer class")
        self.attn = Attention(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        if isinstance(norm_layer, str):
            self.norm2 = eval(norm_layer)(dim, epsilon=epsilon)
        elif isinstance(norm_layer, Callable):
            self.norm2 = norm_layer(dim)
        else:
            raise TypeError(
                "The norm_layer must be str or paddle.nn.layer.Layer class")
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim,
                       hidden_features=mlp_hidden_dim,
                       act_layer=act_layer,
                       drop=drop)

    def forward(self, x):
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PatchEmbed(nn.Layer):
    """ Image to Patch Embedding
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * \
            (img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        self.proj = nn.Conv2D(
            in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x):
        B, C, H, W = x.shape
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."

        x = self.proj(x).flatten(2).transpose((0, 2, 1))
        return x


class FaceViT(Model):
    """ Vision Transformer with support for patch input
    """

    def __init__(self,
                 img_size=112,
                 patch_size=16,
                 in_chans=3,
                 num_features=512,
                 class_num=93431,
                 embed_dim=768,
                 depth=12,
                 num_heads=12,
                 mlp_ratio=4,
                 qkv_bias=False,
                 qk_scale=None,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.,
                 norm_layer='nn.LayerNorm',
                 epsilon=1e-5,
                 mask_ratio=0.1,
                 pfc_config={"model_parallel": True,
                             "sample_ratio": 1.0},
                 **kwargs):
        super().__init__()
        self.class_num = class_num
        self.mask_ratio = mask_ratio

        self.num_features = num_features
        self.embed_dim = embed_dim

        self.patch_embed = PatchEmbed(
            img_size=img_size,
            patch_size=patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches
        self.num_patches = num_patches

        self.pos_embed = self.create_parameter(
            shape=(1, num_patches, embed_dim), default_initializer=zeros_)
        self.add_parameter("pos_embed", self.pos_embed)

        self.mask_token = self.create_parameter(
            shape=(1, 1, embed_dim), default_initializer=zeros_)
        self.add_parameter("mask_token", self.mask_token)

        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = np.linspace(0, drop_path_rate, depth)

        self.blocks = nn.LayerList([
            Block(
                dim=embed_dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=dpr[i],
                norm_layer=norm_layer,
                epsilon=epsilon) for i in range(depth)
        ])

        self.norm = eval(norm_layer)(embed_dim, epsilon=epsilon)

        # features
        self.feature = nn.Sequential(
            nn.Linear(
                in_features=embed_dim * num_patches,
                out_features=embed_dim,
                bias_attr=False),
            nn.BatchNorm1D(
                num_features=embed_dim, epsilon=2e-5),
            nn.Linear(
                in_features=embed_dim,
                out_features=num_features,
                bias_attr=False),
            nn.BatchNorm1D(
                num_features=num_features, epsilon=2e-5))

        pfc_config.update({
            'num_classes': class_num,
            'embedding_size': num_features,
            'name': 'partialfc'
        })
        self.head = PartialFC(**pfc_config)

        trunc_normal_(self.mask_token)
        trunc_normal_(self.pos_embed)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                zeros_(m.bias)
        elif isinstance(m, nn.LayerNorm):
            zeros_(m.bias)
            ones_(m.weight)

    def random_masking(self, x, mask_ratio=0.1):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))

        # noise in [0, 1]
        if x.dtype == paddle.float16:
            noise = paddle.rand((N, L), dtype=paddle.float32).astype(x.dtype)
        else:
            noise = paddle.rand((N, L), dtype=x.dtype)

        # sort noise for each sample
        # ascend: small is keep, large is remove
        ids_shuffle = paddle.argsort(noise, axis=1)
        ids_restore = paddle.argsort(ids_shuffle, axis=1)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]

        x_masked = paddle.take_along_axis(
            x, ids_keep.unsqueeze(-1).tile([1, 1, D]), axis=1)

        # generate the binary mask: 0 is keep, 1 is remove
        mask = paddle.ones([N, L])
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = paddle.take_along_axis(mask, ids_restore, axis=1)

        return x_masked, mask, ids_restore

    def forward_features(self, x):
        B = paddle.shape(x)[0]
        x = self.patch_embed(x)
        x = x + self.pos_embed
        x = self.pos_drop(x)

        if self.training and self.mask_ratio > 0:
            x, _, ids_restore = self.random_masking(x)

        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        if self.training and self.mask_ratio > 0:
            mask_tokens = self.mask_token.tile(
                [x.shape[0], ids_restore.shape[1] - x.shape[1], 1])
            x_ = paddle.concat([x, mask_tokens], axis=1)  # no cls token
            x_ = paddle.take_along_axis(
                x_, ids_restore.unsqueeze(-1).tile([1, 1, x.shape[2]]),
                axis=1)  # unshuffle
            x = x_
        return paddle.reshape(x, [B, self.num_patches * self.embed_dim])

    def forward(self, inputs):
        if isinstance(inputs, dict):
            x = inputs['data']
        else:
            x = inputs
        x.stop_gradient = True
        x = self.forward_features(x)
        y = self.feature(x)

        if not self.training:
            # return embedding feature
            if isinstance(inputs, dict):
                res = {'logits': y}
                if 'targets' in inputs:
                    res['targets'] = inputs['targets']
            else:
                res = y
            return res

        assert isinstance(inputs, dict) and 'targets' in inputs
        y, targets = self.head(y, inputs['targets'])

        return {'logits': y, 'targets': targets}

    def load_pretrained(self, path, rank=0, finetune=False):
        if not os.path.exists(path + '.pdparams'):
            raise ValueError("Model pretrain path {} does not "
                             "exists.".format(path))

        state_dict = paddle.load(path + ".pdparams")

        dist_param_path = path + "_rank{}.pdparams".format(rank)
        if os.path.exists(dist_param_path):
            dist_state_dict = paddle.load(dist_param_path)
            state_dict.update(dist_state_dict)

            # clear
            dist_state_dict.clear()

        if not finetune:
            self.set_dict(state_dict)
            return

        return

    def save(self, path, local_rank=0, rank=0):
        dist_state_dict = collections.OrderedDict()
        state_dict = self.state_dict()
        for name, param in list(state_dict.items()):
            if param.is_distributed:
                dist_state_dict[name] = state_dict.pop(name)

        if local_rank == 0:
            paddle.save(state_dict, path + ".pdparams")

        if len(dist_state_dict) > 0:
            paddle.save(dist_state_dict,
                        path + "_rank{}.pdparams".format(rank))


def FaceViT_tiny_patch9_112(**kwargs):
    model = FaceViT(
        img_size=112,
        patch_size=9,
        embed_dim=256,
        depth=12,
        num_heads=8,
        mlp_ratio=4,
        drop_path_rate=0.1,
        mask_ratio=0.1,
        **kwargs)
    return model


def FaceViT_small_patch9_112(**kwargs):
    model = FaceViT(
        img_size=112,
        patch_size=9,
        embed_dim=512,
        depth=12,
        num_heads=8,
        mlp_ratio=4,
        drop_path_rate=0.1,
        mask_ratio=0.1,
        **kwargs)
    return model


def FaceViT_base_patch9_112(**kwargs):
    model = FaceViT(
        img_size=112,
        patch_size=9,
        embed_dim=512,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        drop_path_rate=0.1,
        mask_ratio=0.1,
        **kwargs)
    return model


def FaceViT_large_patch9_112(**kwargs):
    model = FaceViT(
        img_size=112,
        patch_size=9,
        embed_dim=768,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        drop_path_rate=0.05,
        mask_ratio=0.05,
        **kwargs)
    return model
