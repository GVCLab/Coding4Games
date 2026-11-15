import pytest

from coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer import _dxcam_output_region


def test_dxcam_close_releases_factory_instance_and_discards_cached_frame():
    from types import SimpleNamespace
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer import DxcamRenderer

    renderer = object.__new__(DxcamRenderer)
    calls = []
    renderer.camera = SimpleNamespace(release=lambda: calls.append("release"))
    renderer.last = object()
    renderer.close()
    renderer.close()
    assert calls == ["release"]
    assert renderer.camera is None and renderer.last is None


def test_dxcam_region_is_local_to_secondary_monitor():
    monitors = [
        (0, 0, 2560, 1600),
        (-2560, 67, 0, 1507),
    ]

    output_idx, region = _dxcam_output_region(
        (-1920, 387, 1280, 800), monitors,
    )

    assert output_idx == 1
    assert region == (640, 320, 1920, 1120)


def test_dxcam_region_stays_unchanged_on_primary_monitor():
    monitors = [
        (0, 0, 2560, 1600),
        (-2560, 67, 0, 1507),
    ]

    assert _dxcam_output_region((100, 200, 1280, 720), monitors) == (
        0,
        (100, 200, 1380, 920),
    )


def test_dxcam_region_rejects_a_window_spanning_outputs():
    monitors = [
        (0, 0, 2560, 1600),
        (-2560, 67, 0, 1507),
    ]

    with pytest.raises(ValueError, match="inside one monitor"):
        _dxcam_output_region((-100, 100, 200, 200), monitors)


def desktop_renderer(monkeypatch, scale=1.5):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    import numpy as np
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import renderer

    gui = SimpleNamespace(
        IsWindow=lambda h: True, IsIconic=lambda h: False,
        GetForegroundWindow=lambda: 12,
        GetClientRect=lambda h: (0, 0, 1920, 1080),
        ClientToScreen=lambda h, p: (12, 47),
        WindowFromPoint=lambda p: 12, GetAncestor=lambda h, flag: h,
        GetClassName=lambda h: 'TestOverlay',
    )
    camera = SimpleNamespace(
        start=Mock(), release=Mock(),
        get_latest_frame=Mock(return_value=np.full((1080, 1920, 3), 70, dtype=np.uint8)),
    )
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (1, 42)))
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        EnumDisplayMonitors=lambda: [(1, None, (0, 0, 2560, 1600))]))
    monkeypatch.setitem(sys.modules, 'dxcam', SimpleNamespace(create=lambda **kw: camera))
    monkeypatch.setattr(renderer, 'printwindow_client_scale', lambda h: scale)
    obj = renderer.make_windows_renderer(
        hwnd=12, bbox=(12, 47, 1920, 1080), pid=42, env_fps=10, backend='dxcam')
    return obj, camera, gui


@pytest.mark.parametrize('scale,expected', [(1.5, (1280, 720)), (1.0, (1920, 1080))])
def test_dxcam_factory_preserves_native_viewport_under_desktop_dpi_stretch(monkeypatch, scale, expected):
    obj, camera, gui = desktop_renderer(monkeypatch, scale=scale)
    assert obj.frame_size == expected
    image = obj.screenshot()
    assert image.size == expected
    assert image.getpixel((640, 360)) == (70, 70, 70)
    camera.start.assert_called_once_with(
        region=(12, 47, 1932, 1127), target_fps=10, video_mode=True)


@pytest.mark.parametrize('failure', ['background', 'overlay', 'minimized', 'moved', 'wrong_pid'])
def test_dxcam_rejects_an_invalid_desktop_surface_before_reading(monkeypatch, failure):
    import sys
    obj, camera, gui = desktop_renderer(monkeypatch)
    if failure == 'background': gui.GetForegroundWindow = lambda: 99
    if failure == 'overlay': gui.WindowFromPoint = lambda p: 99
    if failure == 'minimized': gui.IsIconic = lambda h: True
    if failure == 'moved': gui.ClientToScreen = lambda h, p: (14, 47)
    if failure == 'wrong_pid':
        sys.modules['win32process'].GetWindowThreadProcessId = lambda h: (1, 43)
    with pytest.raises(RuntimeError, match='desktop capture'):
        obj.screenshot()
    camera.get_latest_frame.assert_not_called()


def test_dxcam_checks_for_occlusion_after_the_frame_arrives(monkeypatch):
    obj, camera, gui = desktop_renderer(monkeypatch)
    pixels = camera.get_latest_frame.return_value
    def interrupted():
        gui.WindowFromPoint = lambda p: 99
        return pixels
    camera.get_latest_frame.side_effect = interrupted
    with pytest.raises(RuntimeError, match='obscured'):
        obj.screenshot()


def test_dxcam_missing_frame_cannot_substitute_a_previous_observation(monkeypatch):
    obj, camera, gui = desktop_renderer(monkeypatch)
    obj.screenshot()
    camera.get_latest_frame.return_value = None
    with pytest.raises(RuntimeError, match='no frame'):
        obj.screenshot()


@pytest.mark.parametrize('enabled,same_owner', [(False, True), (True, False)])
def test_capture_error_pixels_are_opt_in_and_never_capture_foreign_overlays(
        monkeypatch, tmp_path, enabled, same_owner):
    import sys
    from PIL import ImageGrab
    from unittest.mock import Mock
    obj, camera, gui = desktop_renderer(monkeypatch)
    gui.WindowFromPoint = lambda p: 99
    sys.modules['win32process'].GetWindowThreadProcessId = lambda h: (
        1, 42 if h == 12 or same_owner else 43)
    if enabled:
        monkeypatch.setenv('C4G_CAPTURE_ERROR_EVIDENCE_DIR', str(tmp_path))
    else:
        monkeypatch.delenv('C4G_CAPTURE_ERROR_EVIDENCE_DIR', raising=False)
    grab = Mock()
    monkeypatch.setattr(ImageGrab, 'grab', grab)
    with pytest.raises(RuntimeError, match='obscured'):
        obj.screenshot()
    grab.assert_not_called()
    camera.get_latest_frame.assert_not_called()


@pytest.mark.parametrize('save_fails', [False, True])
def test_owned_capture_error_evidence_remains_fatal_and_is_attempted_once(
        monkeypatch, tmp_path, save_fails):
    import json
    from PIL import Image, ImageGrab
    from unittest.mock import Mock
    obj, camera, gui = desktop_renderer(monkeypatch)
    gui.WindowFromPoint = lambda p: 99
    gui.GetWindowRect = lambda h: (12, 47, 112, 147)
    monkeypatch.setenv('C4G_CAPTURE_ERROR_EVIDENCE_DIR', str(tmp_path))
    grab = Mock(return_value=Image.new('RGB', (1920, 1080)))
    if save_fails:
        grab.side_effect = OSError('diagnostic capture unavailable')
    monkeypatch.setattr(ImageGrab, 'grab', grab)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='obscured'):
            obj.screenshot()
    grab.assert_called_once_with(bbox=(12, 47, 1932, 1127), all_screens=True)
    camera.get_latest_frame.assert_not_called()
    records = list(tmp_path.glob('*.json'))
    assert len(records) == (0 if save_fails else 1)
    if records:
        record = json.loads(records[0].read_text())
        assert record['accepted_observation'] is False
        assert record['same_root_after_capture'] is True
        assert record['game_foreground_after_capture'] is True
        assert (tmp_path / record['image']).exists()


def test_diagnostic_capture_cannot_replace_original_error_when_popup_disappears(
        monkeypatch, tmp_path):
    from PIL import Image, ImageGrab
    obj, camera, gui = desktop_renderer(monkeypatch)
    gui.WindowFromPoint = lambda p: 99
    gui.GetWindowRect = lambda h: (0, 0, 78, 50)
    gui.GetClassName = lambda h: 'CiceroUIWndFrame'
    monkeypatch.setenv('C4G_CAPTURE_ERROR_EVIDENCE_DIR', str(tmp_path))

    def capture_after_disappearance(**kwargs):
        def expired_class(hwnd):
            if hwnd == 99:
                raise OSError('The popup HWND no longer exists')
            return 'GameWindow'
        gui.GetClassName = expired_class
        gui.WindowFromPoint = lambda p: 12
        return Image.new('RGB', (1920, 1080))

    monkeypatch.setattr(ImageGrab, 'grab', capture_after_disappearance)
    with pytest.raises(RuntimeError, match="class='CiceroUIWndFrame'"):
        obj.screenshot()
    camera.get_latest_frame.assert_not_called()
