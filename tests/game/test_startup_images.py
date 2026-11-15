from pathlib import Path

import cv2
import numpy as np
import pytest

from coding4game_gui.benchmark.windows.startup_images import StartupImageChecks


def reference(tmp_path):
    path = tmp_path / 'ready.png'
    cv2.imwrite(str(path), np.full((10, 20, 3), 80, dtype=np.uint8))
    return {'path': path.name, 'rect': [2, 3, 5, 4], 'max_mean_abs_error': 5}


def test_assertion_uses_declared_region_and_never_resizes_the_frame(tmp_path):
    checks = StartupImageChecks([reference(tmp_path)], [20, 10], root=tmp_path)
    frame = np.full((10, 20, 3), 80, dtype=np.uint8)
    frame[0, 0] = 255
    assert checks.matches(frame)
    frame[3:7, 2:7] = 100
    assert not checks.matches(frame)
    assert not checks.matches(np.full((12, 20, 3), 80, dtype=np.uint8))


@pytest.mark.parametrize('patch', [
    {'path': '../outside.png'}, {'path': str(Path(__file__).resolve())},
    {'rect': [-1, 0, 5, 5]}, {'rect': [0, 0, 21, 5]},
    {'rect': [0, 0, 0, 5]}, {'rect': [True, 0, 5, 5]},
    {'max_mean_abs_error': float('nan')}, {'max_mean_abs_error': True},
    {'max_mean_abs_error': -1}, {'max_mean_abs_error': 256},
])
def test_invalid_assertions_fail_before_launch(tmp_path, patch):
    item = reference(tmp_path)
    item.update(patch)
    with pytest.raises(ValueError):
        StartupImageChecks([item], [20, 10], root=tmp_path)


def test_missing_or_wrong_geometry_reference_is_not_a_match(tmp_path):
    item = reference(tmp_path)
    with pytest.raises(ValueError, match='calibrated_client_size'):
        StartupImageChecks([item], [20, 11], root=tmp_path)
    item['path'] = 'absent.png'
    with pytest.raises(ValueError, match='unreadable'):
        StartupImageChecks([item], [20, 10], root=tmp_path)


@pytest.mark.parametrize('patch', [
    {'min_correlation': 0}, {'min_correlation': float('nan')},
    {'min_correlation': True}, {'min_correlation': 1.1},
    {'search_margin': [-1, 0]}, {'search_margin': [0, True]},
    {'search_margin': [10, 0]}, {'rotations': []},
    {'rotations': [float('inf')]}, {'rotations': [31]},
])
def test_correlation_configuration_fails_closed(tmp_path, patch):
    item = reference(tmp_path)
    image = cv2.imread(str(tmp_path / item['path']))
    image[4, 4] = (20, 40, 60)
    cv2.imwrite(str(tmp_path / item['path']), image)
    item.pop('max_mean_abs_error')
    item['min_correlation'] = .9
    item.update(patch)
    with pytest.raises(ValueError):
        StartupImageChecks([item], [20, 10], root=tmp_path)


def test_correlation_search_is_bounded_to_the_declared_native_margin(tmp_path):
    image = np.full((20, 30, 3), 80, dtype=np.uint8)
    patch = np.random.default_rng(7).integers(0, 256, (6, 5, 3), dtype=np.uint8)
    image[5:11, 7:12] = patch
    cv2.imwrite(str(tmp_path / 'ready.png'), image)
    checks = StartupImageChecks([{'path': 'ready.png', 'rect': [7, 5, 5, 6],
        'min_correlation': .99, 'search_margin': [1, 1]}], [30, 20], root=tmp_path)
    moved = np.full_like(image, 80)
    moved[6:12, 8:13] = patch
    assert checks.matches(moved)
    outside = np.full_like(image, 80)
    outside[8:14, 10:15] = patch
    assert not checks.matches(outside)
