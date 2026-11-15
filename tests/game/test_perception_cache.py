"""Exact-image identity and episode lifetime for host OCR reuse."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.perception_cache import EpisodeOcrCache


def test_full_pixels_shape_and_dtype_bind_cached_results():
    calls = []
    def engine(frame):
        calls.append(1)
        return [{'text': str(len(calls)), 'box': [1, 2, 3, 4], 'conf': .9}]
    cache = EpisodeOcrCache(engine)
    directory = Path(cache._directory.name)
    try:
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        first = cache(frame)
        first[0]['text'] = 'mutated by caller'
        assert cache(frame.copy())[0]['text'] == '1'
        frame[1, 1, 0] = 1  # Outside the scorer's existing sampled identity.
        assert cache(frame)[0]['text'] == '2'
        assert cache(frame.reshape(16, 64, 3))[0]['text'] == '3'
        assert cache(frame.astype(np.int16))[0]['text'] == '4'
    finally:
        cache.close()
    assert not directory.exists()


def test_empty_readings_reuse_across_host_request_threads():
    calls = []
    cache = EpisodeOcrCache(lambda frame: calls.append(1) or [])
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(cache, [np.zeros((2, 2, 3), np.uint8)] * 8)) == [[]] * 8
        assert len(calls) == 1 and cache.hits == 7
    finally:
        cache.close()


def test_failed_inference_is_not_cached():
    calls = []
    def engine(frame):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('OCR unavailable')
        return []
    cache = EpisodeOcrCache(engine)
    try:
        frame = np.zeros((2, 2, 3), np.uint8)
        with pytest.raises(RuntimeError, match='OCR unavailable'):
            cache(frame)
        assert cache(frame) == [] and len(calls) == 2
    finally:
        cache.close()
