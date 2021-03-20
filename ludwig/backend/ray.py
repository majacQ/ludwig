#! /usr/bin/env python
# coding=utf-8
# Copyright (c) 2020 Uber Technologies, Inc.
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
# ==============================================================================

import logging
import uuid
from collections import defaultdict, OrderedDict

import dask
import ray
from horovod.ray import RayExecutor
from ray import serve
from ray.util.dask import ray_dask_get

from ludwig.backend.base import Backend, RemoteTrainingMixin
from ludwig.constants import NAME
from ludwig.data.dataframe.dask import DaskEngine
from ludwig.models.predictor import BasePredictor, Predictor
from ludwig.models.trainer import BaseTrainer, RemoteTrainer
from ludwig.utils.misc_utils import sum_dicts
from ludwig.utils.tf_utils import initialize_tensorflow


logger = logging.getLogger(__name__)


def get_dask_kwargs():
    # TODO ray: select this more intelligently,
    #  must be greather than or equal to number of Horovod workers
    return dict(
        parallelism=int(ray.cluster_resources()['CPU'])
    )


def get_horovod_kwargs():
    # TODO ray: https://github.com/horovod/horovod/issues/2702
    resources = [node['Resources'] for node in ray.state.nodes()]
    use_gpu = int(ray.cluster_resources().get('GPU', 0)) > 0

    # Our goal is to maximize the number of training resources we can
    # form into a homogenous configuration. The priority is GPUs, but
    # can fall back to CPUs if there are no GPUs available.
    key = 'GPU' if use_gpu else 'CPU'

    # Bucket the per node resources by the number of the target resource
    # available on that host (equivalent to number of slots).
    buckets = defaultdict(list)
    for node_resources in resources:
        buckets[int(node_resources.get(key, 0))].append(node_resources)

    # Maximize for the total number of the target resource = num_slots * num_workers
    def get_total_resources(bucket):
        slots, resources = bucket
        return slots * len(resources)

    best_slots, best_resources = max(buckets.items(), key=get_total_resources)
    return dict(
        num_slots=best_slots,
        num_hosts=len(best_resources),
        use_gpu=use_gpu
    )


class RayModelServer:
    def __init__(self, remote_model, predictor_kwargs):
        self.model = remote_model.load()
        self.predictor = Predictor(**predictor_kwargs)

    def batch_predict(self, dataset, *args, **kwargs):
        return self.predictor.batch_predict(
            self.model,
            dataset,
            *args,
            **kwargs
        )


class RayRemoteModel:
    def __init__(self, model):
        self.cls, self.args, state = list(model.__reduce__())
        self.state = ray.put(state)

    def load(self):
        obj = self.cls(*self.args)
        obj.__setstate__(ray.get(self.state))
        return obj


class RayRemoteTrainer(RemoteTrainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def train(self, *args, **kwargs):
        results = super().train(*args, **kwargs)
        if results is not None:
            model, *stats = results
            results = (model.get_weights(), *stats)
        return results

    def train_online(self, *args, **kwargs):
        results = super().train_online(*args, **kwargs)
        if results is not None:
            results = results.get_weights()
        return results


class RayTrainer(BaseTrainer):
    def __init__(self, horovod_kwargs, trainer_kwargs):
        # TODO ray: make this more configurable by allowing YAML overrides of timeout_s, etc.
        setting = RayExecutor.create_settings(timeout_s=30)
        self.executor = RayExecutor(setting, **{**get_horovod_kwargs(), **horovod_kwargs})
        self.executor.start(executable_cls=RayRemoteTrainer, executable_kwargs=trainer_kwargs)

    def train(self, model, *args, **kwargs):
        remote_model = RayRemoteModel(model)
        results = self.executor.execute(
            lambda trainer: trainer.train(remote_model.load(), *args, **kwargs)
        )

        weights, *stats = results[0]
        model.set_weights(weights)
        return (model, *stats)

    def train_online(self, model, *args, **kwargs):
        remote_model = RayRemoteModel(model)
        results = self.executor.execute(
            lambda trainer: trainer.train_online(remote_model.load(), *args, **kwargs)
        )

        weights = results[0]
        model.set_weights(weights)
        return model

    @property
    def validation_field(self):
        return self.executor.execute_single(lambda trainer: trainer.validation_field)

    @property
    def validation_metric(self):
        return self.executor.execute_single(lambda trainer: trainer.validation_metric)

    def shutdown(self):
        self.executor.shutdown()


@ray.remote
class MetricCollector(object):
    def __init__(self):
        self.metrics = []

    def add_metrics(self, metrics):
        self.metrics.append(metrics)

    def collect(self):
        return sum_dicts(
            self.metrics,
            dict_type=OrderedDict
        )


class RayPredictor(BasePredictor):
    def __init__(self, horovod_kwargs, predictor_kwargs):
        self.predictor_kwargs = predictor_kwargs
        self.actor_handles = []

    def batch_predict(self, model, dataset, *args, **kwargs):
        remote_model = RayRemoteModel(model)
        predictor_kwargs = self.predictor_kwargs

        def batch_predict_partition(dataset):
            print('BATCH PREDICT PARTITION')
            model = remote_model.load()
            predictor = Predictor(**predictor_kwargs)
            return predictor.batch_predict(model, dataset, *args, **kwargs)

        print('RAY PREDICT')
        return dataset.map_partitions(batch_predict_partition)

    def batch_evaluation(self, model, dataset, *args, **kwargs):
        metric_collector = MetricCollector.remote()
        self.actor_handles.append(metric_collector)

        remote_model = RayRemoteModel(model)
        predictor_kwargs = self.predictor_kwargs

        def batch_evaluate_partition(dataset):
            model = remote_model.load()
            predictor = Predictor(**predictor_kwargs)
            metrics, predictions = predictor.batch_evaluation(
                model, dataset, *args, **kwargs
            )
            ray.get(metric_collector.add_metrics.remote(metrics))
            return predictions

        predictions = dataset.map_partitions(batch_evaluate_partition)
        metrics = ray.get(metric_collector.collect.remote())
        return metrics, predictions

    def batch_collect_activations(self, model, *args, **kwargs):
        raise NotImplementedError()

    def shutdown(self):
        for handle in self.actor_handles:
            ray.kill(handle)


class RayBackend(RemoteTrainingMixin, Backend):
    def __init__(self, horovod_kwargs=None):
        super().__init__()
        self._df_engine = DaskEngine()
        self._horovod_kwargs = horovod_kwargs or {}
        self._tensorflow_kwargs = {}

    def initialize(self):
        try:
            ray.init('auto', ignore_reinit_error=True)
        except ConnectionError:
            logger.info('Initializing new Ray cluster...')
            ray.init(ignore_reinit_error=True)

        dask.config.set(scheduler=ray_dask_get)
        self._df_engine.set_parallelism(**get_dask_kwargs())

    def initialize_tensorflow(self, **kwargs):
        # Make sure we don't claim any GPU resources on the head node
        initialize_tensorflow(gpus=-1)
        self._tensorflow_kwargs = kwargs

    def create_trainer(self, **kwargs):
        executable_kwargs = {**kwargs, **self._tensorflow_kwargs}
        return RayTrainer(self._horovod_kwargs, executable_kwargs)

    def create_predictor(self, **kwargs):
        executable_kwargs = {**kwargs, **self._tensorflow_kwargs}
        return RayPredictor(self._horovod_kwargs, executable_kwargs)

    @property
    def df_engine(self):
        return self._df_engine

    @property
    def supports_multiprocessing(self):
        return False

    def check_lazy_load_supported(self, feature):
        raise ValueError(f'RayBackend does not support lazy loading of data files at train time. '
                         f'Set preprocessing config `in_memory: True` for feature {feature[NAME]}')