# Copyright 2023 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Aggregation Layer for SLModel

"""
from typing import Dict, List, Tuple, Union

import jax.numpy as jnp
import numpy as np
import tensorflow as tf
import torch

import secretflow as sf
from secretflow.device import HEU, PYU, SPU, DeviceObject, PYUObject
from secretflow.ml.nn.sl.agglayer.agg_method import AggMethod
from secretflow.utils.communicate import ForwardData
from secretflow.utils.compressor import Compressor, SparseCompressor
from secretflow.utils.errors import InvalidArgumentError

COMPRESS_DEVICE_LIST = (PYU,)


class AggLayer(object):
    """
    The aggregation layer is situated between Basenet and Fusenet and is responsible for feature fusion, communication compression, and other intermediate layer logic.
    Attributes:
        device_agg: The party do aggregation,it can be a PYU,SPU,etc.
        parties: List of all parties.
        device_y: The party which has fusenet
        agg_method: Aggregation method must inherit from agg_method.AggMethod
        backend: tensorflow or torch
        compressor: Define strategy tensor compression algorithms to speed up transmission.

    """

    def __init__(
        self,
        device_agg: Union[PYU, SPU, HEU],
        parties: List[PYU],
        device_y: PYU,
        agg_method: AggMethod = None,
        backend: str = "tensorflow",
        compressor: Compressor = None,
    ):
        assert isinstance(
            device_agg, (PYU, SPU, HEU)
        ), f'Accepts device in [PYU,SPU,HEU]  but got {type(device_agg)}.'
        if not agg_method and device_agg != device_y:
            raise InvalidArgumentError(f"Default mode, device_agg must to be device_y")
        self.device_agg = device_agg
        self.parties = parties
        self.device_y = device_y
        self.server_data = None
        self.agg_method = agg_method
        self.backend = backend.lower()
        self.compressor = compressor
        self.basenet_output_num = None
        self.hiddens = None
        self.losses = None
        self.fuse_sparse_masks = None
        self.is_compressed = None

    def get_parties(self):
        return self.parties

    def set_basenet_output_num(self, basenet_output_num: int):
        self.basenet_output_num = basenet_output_num

    @staticmethod
    def convert_to_ndarray(*data: List) -> Union[List[jnp.ndarray], jnp.ndarray]:
        def _convert_to_ndarray(hidden):
            # processing data
            if not isinstance(hidden, jnp.ndarray):
                if isinstance(hidden, (tf.Tensor, torch.Tensor)):
                    hidden = jnp.array(hidden.numpy())
                if isinstance(hidden, np.ndarray):
                    hidden = jnp.array(hidden)
            return hidden

        if isinstance(data, Tuple) and len(data) == 1:
            # The case is after packing and unpacking using PYU, a tuple of length 1 will be obtained, if 'num_return' is not specified to PYU.
            data = data[0]
        if isinstance(data, (List, Tuple)):
            return [_convert_to_ndarray(d) for d in data]
        else:
            return _convert_to_ndarray(data)

    @staticmethod
    def convert_to_tensor(hidden: Union[List, Tuple], backend: str):
        if backend == "tensorflow":
            if isinstance(hidden, (List, Tuple)):
                hidden = [tf.convert_to_tensor(d) for d in hidden]

            else:
                hidden = tf.convert_to_tensor(hidden)
        elif backend == "torch":
            if isinstance(hidden, (List, Tuple)):
                hidden = [torch.Tensor(d) for d in hidden]
            else:
                hidden = torch.Tensor(hidden)
        else:
            raise InvalidArgumentError(
                f"Invalid backend, only support 'tensorflow' or 'torch', but got {backend}"
            )
        return hidden

    @staticmethod
    def get_hiddens(f_data):
        if isinstance(f_data, (Tuple, List)):
            if isinstance(f_data[0], ForwardData):
                return [d.hidden for d in f_data]
            else:
                return [d for d in f_data]
        else:
            if isinstance(f_data, ForwardData):
                return f_data.hidden
            else:
                return f_data

    @staticmethod
    def get_reg_loss(f_data: ForwardData):
        if isinstance(f_data, (Tuple, List)):
            if isinstance(f_data[0], ForwardData):
                return [d.losses for d in f_data]
            else:
                return None
        else:
            if isinstance(f_data, ForwardData):
                return f_data.losses
            else:
                return None

    @staticmethod
    def set_forward_data(
        hidden,
        losses,
    ):
        return ForwardData(
            hidden=hidden,
            losses=losses,
        )

    @staticmethod
    def handle_sparse_hiddens(hidden_features, compressor):
        iscompressed = compressor.iscompressed(hidden_features)
        # save fuse_sparse_masks to apply on gradients
        fuse_sparse_masks = None
        if isinstance(compressor, SparseCompressor):
            fuse_sparse_masks = list(
                map(
                    # Get a sparse matrix mask with dtype=bool.
                    # Using <bool> as the dtype will ensure that the data type of gradients after applying the mask does not change.
                    lambda d, compressed: (d != 0) if compressed else None,
                    hidden_features,
                    iscompressed,
                )
            )
        # decompress
        hidden_features = list(
            map(
                lambda d, compressed: compressor.decompress(d) if compressed else d,
                hidden_features,
                iscompressed,
            )
        )
        return hidden_features, fuse_sparse_masks, iscompressed

    @staticmethod
    def handle_sparse_gradients(gradient, sparse_masks, compressor, iscompressed):
        gradient = [g.numpy() for g in gradient]
        # apply fuse_sparse_masks on gradients
        if sparse_masks:
            assert len(sparse_masks) == len(
                gradient
            ), f'length of fuse_sparse_masks and gradient mismatch: {len(sparse_masks)} - {len(gradient)}'

            def apply_mask(m, d):
                if m is not None:
                    return m.multiply(d).tocsr()
                return d

            gradient = list(map(apply_mask, sparse_masks, gradient))
        else:
            gradient = list(
                map(lambda d, compressed: compressor.compress(d) if compressed else d),
                gradient,
                iscompressed,
            )
        return gradient

    def split_to_parties(self, data: List) -> List[PYUObject]:
        assert (
            self.basenet_output_num is not None
        ), "Agglayer should know output num of each participates"
        assert len(data) == sum(
            self.basenet_output_num.values()
        ), f"data length in backward = {len(data)} is not consistent with basenet need = {sum(self.basenet_output_num.values())},"

        result = []
        start_idx = 0
        for p in self.parties:
            data_slice = data[start_idx : start_idx + self.basenet_output_num[p]]
            result.append(data_slice)
            start_idx = start_idx + start_idx + self.basenet_output_num[p]
        return result

    def collect(self, data: Dict[PYU, DeviceObject]) -> List[DeviceObject]:
        """Collect data from participates"""
        assert data, 'Data to aggregate should not be None or empty!'

        # Record the values of fields in ForwardData except for hidden
        self.losses = []

        coverted_data = []
        for device, f_datum in data.items():
            hidden = device(self.get_hiddens)(f_datum)
            loss = device(self.get_reg_loss)(f_datum)
            # transfer other fields to device_y
            self.losses.append(loss.to(self.device_y))

            # aggregate hiddens on device_agg, then push to device_y
            hidden = device(self.convert_to_ndarray)(hidden)
            # do compress before send to device agg
            if isinstance(self.device_agg, COMPRESS_DEVICE_LIST) and self.compressor:
                hidden = device(self.compressor.compress)(hidden)
            coverted_data.append(hidden)
        # do transfer
        server_data = [d.to(self.device_agg) for d in coverted_data]

        # do decompress after recieve data from each parties
        if isinstance(self.device_agg, COMPRESS_DEVICE_LIST) and self.compressor:
            server_data = [
                self.device_agg(self.compressor.decompress)(d) for d in server_data
            ]
            return server_data
        return server_data

    def scatter(self, data: ForwardData) -> Dict[PYU, DeviceObject]:
        """Send ForwardData to participates"""
        # do compress before send to participates
        if isinstance(self.device_agg, COMPRESS_DEVICE_LIST) and self.compressor:
            data = [self.device_agg(self.compressor.compress)(datum) for datum in data]
        # send
        result = {}
        for p, d in zip(self.parties, data):
            datum = d.to(p)
            # do decompress after recieve from device agg
            if isinstance(self.device_agg, COMPRESS_DEVICE_LIST) and self.compressor:
                datum = p(self.compressor.decompress)(datum)
            # convert to tensor
            datum = p(self.convert_to_tensor)(datum, self.backend)
            result[p] = datum
        return result

    def forward(
        self,
        data: Dict[PYU, DeviceObject],
        axis=0,
        weights=None,
    ) -> DeviceObject:
        """Forward aggregate the embeddings calculated by all parties according to the agg_method

        Args:
            data: A dict contain PYU and ForwardData
            axis: Along which axis will the merge be done, default 0
            weights: weight of each side, default to be none
        Returns:
            agg_data_tensor: return aggregated result in tensor type
        """
        assert data, 'Data to aggregate should not be None or empty!'
        if self.agg_method:
            server_data = self.collect(data)
            if isinstance(weights, (list, tuple)):
                weights = [
                    w.to(self.device_agg) if isinstance(w, DeviceObject) else w
                    for w in weights
                ]
            self.hiddens = server_data
            # agg hiddens
            agg_hiddens = self.device_agg(
                self.agg_method.forward, static_argnames="axis"
            )(*server_data, axis=axis, weights=weights)

            # send to device y
            agg_hiddens = agg_hiddens.to(self.device_y)

            # TODO: This is not dead code, it will automatically take effect after agglayer supports sparse calculation. @juxing
            if self.compressor:
                agg_hiddens, fuse_sparse_masks, is_compressed = self.device_y(
                    self.handle_sparse_hiddens,
                    num_returns=3,
                )([agg_hiddens], self.compressor)
                self.fuse_sparse_masks = fuse_sparse_masks
                self.is_compressed = is_compressed

            # convert to tensor on device y
            agg_hidden_tensor = self.device_y(self.convert_to_tensor)(
                agg_hiddens, self.backend
            )

            # make new ForwardData and return
            agg_forward_data = self.device_y(self.set_forward_data)(
                agg_hidden_tensor, self.losses
            )

            return agg_forward_data
        else:
            data = [datum.to(self.device_y) for datum in data.values()]
            if self.compressor:
                data, fuse_sparse_masks, is_compressed = self.device_y(
                    self.handle_sparse_hiddens,
                    num_returns=3,
                )(data, self.compressor)
                self.fuse_sparse_masks = fuse_sparse_masks
                self.is_compressed = is_compressed

            return data

    def backward(
        self,
        gradient: DeviceObject,
        weights=None,
    ) -> Dict[PYU, DeviceObject]:
        """Backward split the gradients to all parties according to the agg_method

        Args:
            gradient: Gradient, tensor format calculated from fusenet
            weights: Weights of each side, default to be none
        Returns:
            scatter_gragient: Return gradients computed following the agg_method.backward and send to each parties
        """
        assert gradient, 'gradient to aggregate should not be None or empty!'
        if self.agg_method:
            if self.compressor:
                gradient = self.device_y(self.handle_sparse_gradients)(
                    gradient,
                    self.fuse_sparse_masks,
                    self.compressor,
                    self.is_compressed,
                )
            if isinstance(gradient, DeviceObject):
                gradient = gradient.to(self.device_agg)
            if isinstance(weights, (Tuple, List)):
                weights = [
                    w.to(self.device_agg) if isinstance(w, DeviceObject) else w
                    for w in weights
                ]
            # convert to numpy
            gradient_numpy = self.device_agg(self.convert_to_ndarray)(gradient)
            if isinstance(gradient_numpy, DeviceObject):
                gradient_numpy = [gradient_numpy]
            if isinstance(self.device_agg, SPU):
                # do agg layer backward
                p_gradient = self.device_agg(
                    self.agg_method.backward,
                    static_argnames='parties_num',
                    num_returns_policy=sf.device.SPUCompilerNumReturnsPolicy.FROM_USER,
                    user_specified_num_returns=len(self.parties),
                )(
                    *gradient_numpy,
                    weights=weights,
                    inputs=self.hiddens,
                    parties_num=len(self.parties),
                )
            else:
                p_gradient = self.device_agg(
                    self.agg_method.backward,
                    num_returns=len(self.parties),
                )(
                    *gradient_numpy,
                    weights=weights,
                    inputs=self.hiddens,
                    parties_num=len(self.parties),
                )
            scatter_g = self.scatter(p_gradient)
        else:
            assert (
                gradient.device == self.device_y
            ), "The device of gradients(PYUObject) must located on party device_y "
            if self.compressor:
                gradient = self.device_y(self.handle_sparse_gradients)(
                    gradient,
                    self.fuse_sparse_masks,
                    self.compressor,
                    self.is_compressed,
                )
            p_gradient = self.device_y(
                self.split_to_parties,
                num_returns=len(self.parties),
            )(
                gradient,
            )
            scatter_g = {}
            for p, g in zip(self.parties, p_gradient):
                p_g = g.to(p)
                scatter_g[p] = p_g
        return scatter_g
