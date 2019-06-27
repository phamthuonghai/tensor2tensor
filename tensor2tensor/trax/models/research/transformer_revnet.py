# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
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

"""Transformer Models."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import jax

from tensor2tensor.trax import backend
from tensor2tensor.trax import layers as tl
from tensor2tensor.trax.layers.combinators import _pop_rng_and_split


# Layers are always CamelCase, but functions in general are snake_case
# pylint: disable=invalid-name


class Map(tl.Layer):
  """Combinator for applying a layer to a list or tuple.

  Args:
    layer: a layer to apply to each element.

  Returns:
    A new layer representing mapping layer to all elements of the input.
  """

  def __init__(self, layer, sections=1, check_shapes=True):
    super(Map, self).__init__()
    if layer is None or isinstance(layer, (list, tuple)):
      layer = tl.Serial(layer)
    self._layer = layer
    # Generally a Map should be applied to lists where all elements have
    # the same shape -- because self._layer will only be initialized once
    # and it could have different parameters for different shapes. But there
    # are valid cases -- e.g., when self._layer has no parameters -- where we
    # can apply Map to different shapes -- set check_shapes=False in such cases.
    self._check_shapes = check_shapes
    self._sections = sections

  def n_inputs(self):
    """Specifies how many data tensors this layer expects as input."""
    return self._sections

  def n_outputs(self):
    """Specifies how many data tensors this layer promises as output."""
    return self._sections

  def call(self, inputs, params=(), **kwargs):
    rngs = _pop_rng_and_split(kwargs, len(inputs))
    result = [self._layer(x, params=params, rng=r, **kwargs)
              for x, r in zip(inputs, rngs)]
    return tuple(result)

  def new_parameters(self, input_shape, input_dtype, rng):
    first_shape = input_shape[0]
    if self._check_shapes:
      for shape in input_shape:
        if shape != first_shape:
          raise ValueError('Map layer can only be applied to list of elements '
                           'with the same shapes. Shapes: %s' % str(shape))
    return self._layer.initialize(first_shape, input_dtype[0], rng)


def FeedForward(d_feature, d_feedforward, dropout, mode):
  """Feed-forward block with layer normalization at start."""
  # TODO(kitaev): add dropout. Dropout is typically performed by adding noise to
  # the activations, but when the size of the activations is very large it is
  # more efficient to add noise to the *parameters* instead.
  del dropout, mode
  return [
      tl.LayerNorm(),
      tl.Dense(d_feedforward),
      tl.Relu(),
      tl.Dense(d_feature),
  ]


class ReversibleLayerMixin(object):
  """Reversible Layer Mixin."""

  def inverse_and_vjp(self, output, ct, params=(), **kwargs):
    """Backward pass: computes the inverse of a layer and propagates gradients.

    Args:
      output: Output activations; can be a (possibly nested) tuple.
      ct: gradient signal (cotangent) computed based on subsequent layers. If
          None, no gradients are propagated. Otherwise the structure and shape
          must match the output.
      params: layer parameters
      **kwargs: kwargs for the layer

    Returns:
      A tuple (x, x_ct), where x is the reconstructed input and x_ct is the
      gradient signal for the input. If ct is None, x_ct will also be None.
    """
    if ct is None:
      # Subclasses must override inverse_and_vjp, but in the case where ct is
      # not None there is an unoptimized implementation below that they can
      # delegate to.
      raise NotImplementedError

    # Note: jax.vjp does not allow us to use **kwargs in the signature here.
    def _do_call(x, params, kwargs):
      return super(ReversibleLayerMixin, self).__call__(x, params, **kwargs)

    reconstructed_x, must_be_none = self.inverse_and_vjp(
        output, None, params, **kwargs)
    assert must_be_none is None
    _, vjpfun = jax.vjp(_do_call, reconstructed_x, params, kwargs)
    input_ct = vjpfun(ct)
    return reconstructed_x, input_ct

  def __call__(self, x, params=(), **kwargs):
    assert backend.get_name() == 'jax', (
        'Reversible layers are only supported in JAX')

    # Retrieve shared parameters (cf. tl.Layer.__call__)
    super(ReversibleLayerMixin, self).__call__(x, params, **kwargs)
    if params is () and self._params:  # pylint: disable=literal-comparison
      # TODO(kitaev): Figure out why parameter sharing doesn't work (if this
      # explicit error isn't thrown, a jax tracer error occurs instead)
      raise NotImplementedError(
          'Parameter sharing between reversible layers is not implemented.')

    @jax.custom_transforms
    def do_call(x, params, kwargs):
      return super(ReversibleLayerMixin, self).__call__(x, params, **kwargs)

    def do_call_vjp(x, params, kwargs):
      output = super(ReversibleLayerMixin, self).__call__(x, params, **kwargs)
      def vjpfun(ct):
        _, input_ct = self.inverse_and_vjp(output, ct, params, **kwargs)
        return input_ct

      return output, vjpfun

    jax.defvjp_all(do_call, do_call_vjp)
    return do_call(x, params, kwargs)


class Split(tl.Layer):
  """Splits the input into sections along an axis."""

  def __init__(self, sections=2, axis=-1):
    super(Split, self).__init__()
    self._sections = sections
    self._axis = axis

  def call(self, inputs, params=(), **kwargs):
    del params, kwargs
    return tuple(backend.numpy.split(inputs, self._sections, self._axis))

  def new_parameters(self, input_shapes, input_dtype, rng):
    return ()

  def n_inputs(self):
    """Specifies how many data tensors this layer expects as input."""
    return 1

  def n_outputs(self):
    """Specifies how many data tensors this layer promises as output."""
    return self._sections


@tl.layer()
def Chunk(x, params, sections=2, **kwargs):
  del params, kwargs
  assert x.shape[1] % sections == 0
  return backend.numpy.reshape(x, (
      x.shape[0] * sections,
      x.shape[1] // sections,
      ) + x.shape[2:])


@tl.layer()
def Unchunk(x, params, sections=2, **kwargs):
  del params, kwargs
  assert x.shape[0] % sections == 0
  return backend.numpy.reshape(x, (
      x.shape[0] // sections,
      x.shape[1] * sections,
      ) + x.shape[2:])


class ReversibleHalfResidual(ReversibleLayerMixin, tl.Serial):
  """Half of a RevNet-style residual (only updates part of the hidden state)."""

  def __init__(self, residual_layers):
    self.compute_residual = tl.Serial([
        # (x1_or_y1, x2) -> (x2, x1_or_y1, x2)
        tl.Parallel([], tl.Dup()),
        tl.Swap(),
        tl.Parallel(residual_layers, [], []),
    ])

    layers = [
        self.compute_residual,
        tl.Parallel(tl.Add(), [])
    ]
    super(ReversibleHalfResidual, self).__init__(layers)

    self.subtract_top = tl.Parallel(tl.SubtractTop(), [])
    self.reverse_layers = [self.compute_residual, self.subtract_top]

  def inverse_and_vjp(self, output, ct, params=(), **kwargs):
    rng = kwargs.pop('rng', None)
    rngs = (None,) * self._n_layers
    if rng is not None:
      rngs = backend.random.split(rng, self._n_layers)

    if ct is None:
      reconstructed_x = output
      # Note that self.sublayers() aligns exactly with self.reverse_layers in
      # terms of parameter and rng usage, so no re-ordering is required.
      for layer, p, rng in zip(self.reverse_layers, params, rngs):
        reconstructed_x = layer(reconstructed_x, p, rng=rng, **kwargs)
      return reconstructed_x, None
    else:
      # Note: jax.vjp does not allow us to use **kwargs in the signature here.
      def call_compute_residual(x, params, kwargs):
        return self.compute_residual(x, params, **kwargs)

      assert len(ct) == 2
      ct = ((ct[0], ct[0], ct[1]))

      compute_residual_kwargs = kwargs.copy()
      compute_residual_kwargs['rng'] = rngs[0]
      stack_with_residual, vjpfun = jax.vjp(
          call_compute_residual, output, params[0], compute_residual_kwargs)
      reconstructed_x = self.subtract_top(
          stack_with_residual, params[-1], rng=rngs[-1], **kwargs)

      x_ct, residual_params_ct, kwargs_ct = vjpfun(ct)
      return reconstructed_x, (x_ct, (residual_params_ct, ()), kwargs_ct)


class ReversibleSwap(ReversibleLayerMixin, tl.Swap):
  """Swap the first two element on the stack."""

  def inverse_and_vjp(self, output, ct, params=(), **kwargs):
    if ct is None:
      # Swap is its own inverse
      return self.call(output, params, **kwargs), None
    else:
      return super(ReversibleSwap, self).inverse_and_vjp(
          output, ct, params, **kwargs)


def ReversibleResidual(layers_a, layers_b):
  """RevNet-style reversible residual layer."""
  return [
      ReversibleHalfResidual(layers_a),  # (x1, x2) -> (z1, x2)
      ReversibleSwap(),  # (z1, x2) -> (x2, z1)
      ReversibleHalfResidual(layers_b),  # (x2, z1) -> (y2, z1)
      ReversibleSwap(),  # (y2, z1) -> (z1, y2); where y1 := z1
  ]


class ReversibleSerial(ReversibleLayerMixin, tl.Serial):
  """A reversible version of tl.Serial (requires reversible sub-layers)."""

  def __init__(self, *layers):
    super(ReversibleSerial, self).__init__(*layers)

    # Note that sublayers has already been flattened to remove nested lists.
    for i, layer in enumerate(self.sublayers()):
      if not isinstance(layer, ReversibleLayerMixin):
        raise ValueError(
            'Sub-layer {} of ReversibleSerial is not reversible: {}'.format(
                i, layer))

  def inverse_and_vjp(self, output, ct, params=(), **kwargs):
    rng = kwargs.pop('rng', None)
    rngs = (None,) * self._n_layers
    if rng is not None:
      rngs = backend.random.split(rng, self._n_layers)

    layer_val = output
    if ct is not None:
      layer_ct = ct
      params_ct = []
    for layer, p, rng in reversed(zip(self.sublayers(), params, rngs)):
      layer_val, layer_ct = layer.inverse_and_vjp(
          layer_val, layer_ct, p, rng=rng, **kwargs)
      if ct is not None:
        layer_ct, p_ct, kwargs_ct = layer_ct
        params_ct.insert(0, p_ct)

    # TODO(kitaev): Handle kwargs_ct properly. However, kwargs generally only
    # contains the rng, which is non-differentiable.
    for k in kwargs:
      if k != 'rng':
        raise NotImplementedError(
            'ReversibleSerial does not support differentiation wrt kwargs,'
            'and the key {} is not known to be non-differentiable.'.format(k))

    if ct is not None:
      return layer_val, (layer_ct, params_ct, kwargs_ct)
    else:
      return layer_val, None


def DecoderBlock(d_feature, d_feedforward, n_heads, n_attention_chunks,
                 dropout, mode):
  """Reversible transformer decoder layer.

  Args:
    d_feature: int:  depth of embedding
    d_feedforward: int: depth of feed-forward layer
    n_heads: int: number of attention heads
    n_attention_chunks: int: number of chunks for memory-efficient attention
    dropout: float: dropout rate (how much to drop out)
    mode: str: 'train' or 'eval'

  Returns:
    the layer.
  """
  self_attention = [
      tl.LayerNorm(),
      tl.Dup(),
      tl.Parallel([], tl.CausalMask(axis=-2)),  # Create mask.
      # TODO(kitaev): add dropout
      tl.Attention(d_feature, n_heads=n_heads, dropout=None, mode=mode),
      tl.Parallel([], tl.Drop()),  # Drop mask.
  ]

  # TODO(kitaev): Memory-efficient attention. This chunking is temporary.
  self_attention = [
      Chunk(sections=n_attention_chunks),  # pylint: disable=no-value-for-parameter
      self_attention,
      Unchunk(sections=n_attention_chunks),  # pylint: disable=no-value-for-parameter
  ]

  feed_forward = [
      FeedForward(d_feature, d_feedforward, dropout, mode=mode),
  ]
  return [
      ReversibleResidual([self_attention], [feed_forward]),
  ]


def TransformerRevnetLM(vocab_size,
                        d_feature=512,
                        d_feedforward=2048,
                        n_layers=6,
                        n_heads=8,
                        dropout=0.1,
                        max_len=2048,
                        n_chunks=32,
                        n_attention_chunks=8,
                        mode='train'):
  """Reversible transformer language model (only uses a decoder, no encoder).

  Args:
    vocab_size: int: vocab size
    d_feature: int:  depth of *each half* of the two-part features
    d_feedforward: int: depth of feed-forward layer
    n_layers: int: number of decoder layers
    n_heads: int: number of attention heads
    dropout: float: dropout rate (how much to drop out)
    max_len: int: maximum symbol length for positional encoding
    n_chunks: int: number of chunks (must match input pipeline)
    n_attention_chunks: int: number of chunks for memory-efficient attention
    mode: str: 'train' or 'eval'

  Returns:
    the layer.
  """
  positional_embedder = [
      tl.Embedding(d_feature, vocab_size),
      # TODO(kitaev): add dropout
      tl.PositionalEncoding(max_len=max_len),
  ]
  return tl.Model(
      tl.Concatenate(n_items=n_chunks),
      tl.ShiftRight(),
      positional_embedder,
      tl.Dup(),
      ReversibleSerial([
          DecoderBlock(d_feature, d_feedforward, n_heads, n_attention_chunks,
                       dropout, mode)
          for _ in range(n_layers)
      ]),
      tl.Parallel(tl.LayerNorm(), tl.LayerNorm()),
      tl.Concatenate(),
      Split(sections=n_chunks, axis=-2),  # pylint: disable=no-value-for-parameter
      Map([
          tl.Dense(vocab_size),
          tl.LogSoftmax(),
      ], sections=n_chunks),
  )

