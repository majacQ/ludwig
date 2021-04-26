import logging
import os
import uuid
from pathlib import Path

from ludwig.constants import CHECKSUM
from ludwig.data.cache.util import calculate_checksum
from ludwig.utils import data_utils
from ludwig.utils.fs_utils import path_exists


logger = logging.getLogger(__name__)


class CacheManager(object):
    def __init__(self, dataset_manager, cache_dir=None):
        self._dataset_manager = dataset_manager
        self._cache_dir = cache_dir

    def put_dataset(self, input_fname, config, processed, skip_save_processed_input):
        if not self.can_cache(input_fname, config, skip_save_processed_input):
            return processed

        key = self.get_cache_key(input_fname, config)
        training_set, test_set, validation_set, training_set_metadata = processed

        logger.info('Writing train set metadata')
        data_utils.save_json(
            self.get_cache_path(input_fname, key, 'meta', 'json'),
            training_set_metadata
        )

        logger.info('Writing preprocessed training set cache')
        training_set = self.save(
            self.get_cache_path(input_fname, key),
            training_set,
            config,
            training_set_metadata,
        )

        if test_set is not None:
            logger.info('Writing preprocessed test set cache')
            test_set = self.save(
                self.get_cache_path(input_fname, key, 'test'),
                test_set,
                config,
                training_set_metadata,
            )

        if validation_set is not None:
            logger.info('Writing preprocessed validation set cache')
            validation_set = self.save(
                self.get_cache_path(input_fname, key, 'val'),
                validation_set,
                config,
                training_set_metadata,
            )

        return training_set, test_set, validation_set, training_set_metadata

    def get_dataset(self, input_fname, config):
        key = self.get_cache_key(input_fname, config)
        training_set_metadata_fp = self.get_cache_path(
            input_fname, key, 'meta', 'json'
        )

        if path_exists(training_set_metadata_fp):
            cache_training_set_metadata = data_utils.load_json(
                training_set_metadata_fp
            )

            dataset_fp = self.get_cache_path(input_fname, key)
            test_fp = self.get_cache_path(input_fname, key, 'test')
            val_fp = self.get_cache_path(input_fname, key, 'val')
            valid = key == cache_training_set_metadata.get(CHECKSUM) and path_exists(dataset_fp)
            return valid, cache_training_set_metadata, dataset_fp, test_fp, val_fp

        return None

    def get_cache_key(self, input_fname, config):
        if input_fname is None:
            # TODO(travis): could try hashing the in-memory dataset, but this is tricky for Dask
            return str(uuid.uuid1())
        return calculate_checksum(input_fname, config)

    def get_cache_path(self, input_fname, key, tag=None, ext=None):
        tag = tag or Path(input_fname).stem
        ext = ext or self.data_format
        cache_fname = f'{key}.{tag}.{ext}'
        if self._cache_dir is None:
            cache_fname = f'{tag}.{ext}'
        return os.path.join(self.get_cache_directory(input_fname), cache_fname)

    def get_cache_directory(self, input_fname):
        if self._cache_dir is None:
            if input_fname is not None:
                return os.path.dirname(input_fname)
            return '.'
        return self._cache_dir

    def save(self, cache_path, dataset, config, training_set_metadata):
        return self._dataset_manager.save(cache_path, dataset, config, training_set_metadata)

    def can_cache(self, input_fname, config, skip_save_processed_input):
        return self._dataset_manager.can_cache(input_fname, config, skip_save_processed_input)

    @property
    def data_format(self):
        return self._dataset_manager.data_format