# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD 3-Clause license found in the
# LICENSE file in the root directory of this source tree.
from typing import Any, Dict, Tuple

import torch

from float8_experimental.float8_python_api import addmm_float8_unwrapped
from float8_experimental.float8_tensor import (
    Float8Tensor,
    merge_mm_configs,
    ScaledMMConfig,
)
from float8_experimental.float8_utils import is_row_major
from torch.utils._pytree import tree_map

aten = torch.ops.aten
c10d_functional = torch.ops.c10d_functional
_c10d_functional = torch.ops._c10d_functional
FLOAT8_OPS_TABLE: Dict[Any, Any] = {}


def implements(aten_ops):
    """Register aten ops to the float8 op table"""

    def decorator(func):
        for op in aten_ops:
            FLOAT8_OPS_TABLE[op] = func
        return func

    return decorator


@implements(
    [
        aten.view.default,
        aten._unsafe_view.default,
        aten.t.default,
        aten.as_strided.default,
        aten.clone.default,
        aten.detach.default,
    ]
)
def float8_desugar_op(aten_op, args, kwargs=None):
    new_data = aten_op(args[0]._data, *args[1:], **kwargs)
    return Float8Tensor(
        new_data, args[0]._scale, args[0]._orig_dtype, args[0]._mm_config
    )


@implements([aten.split.Tensor])
def float8_split(aten_op, args, kwargs=None):
    new_data_tensors = aten_op(args[0]._data, *args[1:], **kwargs)

    def make_float8(data):
        return Float8Tensor(
            data, args[0]._scale, args[0]._orig_dtype, args[0]._mm_config
        )

    out = map(make_float8, new_data_tensors)
    return list(out)


# Errors cant `cat_cuda float8 e4m3fn`
@implements([aten.cat.default])
def float8_cat(aten_op, args, kwargs=None):
    chunked_tensors: Tuple[Float8Tensor] = args[0]

    orig_dtype = chunked_tensors[0]._orig_dtype
    scale = chunked_tensors[0]._scale
    mm_config = chunked_tensors[0]._mm_config
    fp8_dtype = chunked_tensors[0]._data.dtype
    chunk_data = []
    for chunk in chunked_tensors:
        assert isinstance(
            chunk, Float8Tensor
        ), "Expecting all chunks to be of type Float8Tensor"
        assert (
            chunk._orig_dtype == orig_dtype
        ), "Expecting all chunks to be of the same dtype"
        assert (
            chunk._scale is scale
        ), "Expecting all chunks to have thee same scale as a result of a split"
        assert (
            chunk._mm_config is mm_config
        ), "Expecting all chunks to have thee same mm config as a result of a split"
        assert (
            chunk._data.dtype == fp8_dtype
        ), "Expecting all chunks to be of the same dtype as a result of a split"
        chunk_data.append(chunk._data.view(torch.uint8))

    new_data = aten_op(chunk_data, *args[1:], **kwargs)
    new_data = new_data.view(fp8_dtype)
    return Float8Tensor(new_data, scale, orig_dtype, mm_config)


@implements([aten.sum.dim_IntList])
def float8_cast_up_op(aten_op, args, kwargs=None):
    """Be careful with this function, this is a "fallback" op that
    casts the output of the op to the original precision. And performs the op.

    We currently need this to support the backward for admmm bias.
    "addmm" -> out
    "hp_gradBias" <-"sum" <- "identity" <- gradOut <- "hp_gradOut"
    """

    def unwrap(x):
        if isinstance(x, Float8Tensor):
            return x.to_original_precision()
        return x

    new_args = tree_map(unwrap, args)
    new_kwargs = tree_map(unwrap, kwargs)
    return aten_op(*new_args, **new_kwargs)


def preprocess_addmm(a: Float8Tensor, b: Float8Tensor):
    a_data = a._data
    a_scale = a._scale
    b_data = b._data

    if not is_row_major(a_data.stride()):
        a_data = a_data.contiguous()
    if is_row_major(b_data.stride()):
        b_data = b_data.t().contiguous().t()
    b_scale = b._scale
    return a_data, a_scale, b_data, b_scale


@implements([aten.mm.default, aten.matmul.default])
def float8_mm(aten_op, args, kwargs=None):
    a = args[0]
    b = args[1]

    assert isinstance(a, Float8Tensor) and isinstance(
        b, Float8Tensor
    ), "Expecting  both Float8Tensor for mm inputs but found {} and {}".format(
        type(a), type(b)
    )
    a_data, a_scale, b_data, b_scale = preprocess_addmm(a, b)
    output_dtype = a._orig_dtype
    a_mm_config: ScaledMMConfig = a._mm_config
    b_mm_config: ScaledMMConfig = b._mm_config
    mm_config: ScaledMMConfig = merge_mm_configs(a_mm_config, b_mm_config)
    if mm_config.emulate:
        return torch.ops.aten.mm_float8_emulated(
            a._data, a._scale, b._data, b._scale, output_dtype
        )[0]
    tensor_out, amax = addmm_float8_unwrapped(
        a_data,
        a_scale,
        b_data,
        b_scale,
        output_dtype,
        output_scale=None,
        bias=None,
        use_fast_accum=mm_config.use_fast_accum,
    )
    return tensor_out


@implements([aten.addmm.default])
def float8_addmm(aten_op, args, kwargs=None):
    assert (
        isinstance(args[0], torch.Tensor)
        and isinstance(args[1], Float8Tensor)
        and isinstance(args[2], Float8Tensor)
    )
    bias = args[0]
    a = args[1]
    b = args[2]
    a_data, a_scale, b_data, b_scale = preprocess_addmm(a, b)
    output_dtype = a._orig_dtype
    assert bias.dtype == output_dtype, "bias dtype must match output dtype"
    a_mm_config: ScaledMMConfig = a._mm_config
    b_mm_config: ScaledMMConfig = b._mm_config
    mm_config: ScaledMMConfig = merge_mm_configs(a_mm_config, b_mm_config)
    if mm_config.emulate:
        out = torch.ops.aten.mm_float8_emulated(
            a._data, a._scale, b._data, b._scale, output_dtype
        )[0]
        return out + bias
    tensor_out, amax = addmm_float8_unwrapped(
        a_data,
        a_scale,
        b_data,
        b_scale,
        output_dtype,
        output_scale=None,
        bias=bias,
        use_fast_accum=mm_config.use_fast_accum,
    )
    return tensor_out


@implements([aten.is_same_size.default])
def float8_is_same_size(aten_op, args, kwargs=None):
    return args[0].shape == args[1].shape


@implements([aten._to_copy.default])
def autocast_to_copy(aten_op, args, kwargs=None):
    """This gets called when running matmul under autocast
    when the input is a Float8Tensor, presenting as a fp32
    tensor.
    """
    assert isinstance(args[0], Float8Tensor)
    assert (
        len(kwargs) == 1 and "dtype" in kwargs
    ), "Only support dtype kwarg for autocast"
    assert kwargs["dtype"] in {
        torch.float16,
        torch.bfloat16,
    }, "Only support floating point conversion for autocast w/ Float8Tensor"
    return Float8Tensor(
        args[0]._data, args[0]._scale, kwargs["dtype"], args[0]._mm_config
    )


@implements(
    [
        c10d_functional.all_gather_into_tensor.default,
        _c10d_functional.all_gather_into_tensor.default,
    ]
)
def allgather_fp8(aten_op, args, kwargs=None):
    """
    override funcol with FP8 handling
    """
    fp8_input = args[0]
    assert isinstance(
        fp8_input, Float8Tensor
    ), f"expecting a Float8Tensor for allgather but found {type(fp8_input)}"

    fp8_data = fp8_input._data
    fp8_data = fp8_data.view(torch.uint8)
    fp8_data = fp8_data.contiguous()
    fp8_out = aten_op(fp8_data, *args[1:], **kwargs)
    fp8_out = torch.ops._c10d_functional.wait_tensor(fp8_out)
    fp8_out = fp8_out.view(fp8_input._data.dtype)
    return Float8Tensor(
        fp8_out, fp8_input._scale, fp8_input._orig_dtype, fp8_input._mm_config
    )


@implements([c10d_functional.wait_tensor.default, _c10d_functional.wait_tensor.default])
def wait_tensor_fp8(aten_op, args, kwargs=None):
    fp8_input = args[0]
    assert isinstance(fp8_input, Float8Tensor)

    fp8_data = fp8_input._data
    fp8_out = aten_op(fp8_data, *args[1:], **kwargs)
    return Float8Tensor(
        fp8_out, fp8_input._scale, fp8_input._orig_dtype, fp8_input._mm_config
    )
