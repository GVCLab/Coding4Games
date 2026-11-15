from unittest.mock import Mock

from PIL import Image
import pytest

from coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer import PrintWindowRenderer


def renderer(*, visible=True, foreground=True, occluded=False):
    obj = PrintWindowRenderer.__new__(PrintWindowRenderer)
    obj.hwnd = 12
    obj.pid = 42
    obj.visible_fallback = visible
    obj._crop = (12, 47, 1292, 767)
    obj._win32gui = Mock()
    obj._win32gui.IsWindow.return_value = True
    obj._win32gui.GetForegroundWindow.return_value = 12 if foreground else 99
    obj._win32gui.GetClientRect.return_value = (0, 0, 1920, 1080)
    obj._win32gui.ClientToScreen.return_value = (12, 70)
    obj._win32gui.WindowFromPoint.return_value = 99 if occluded else 12
    obj._win32gui.GetAncestor.side_effect = lambda hwnd, _: hwnd
    obj._refresh_geometry_if_resized = Mock()
    obj._grab = Mock(return_value=Image.new('RGB', (1280, 720)))
    return obj


def test_black_printwindow_uses_current_visible_client_at_native_size(monkeypatch):
    grab = Mock(return_value=Image.new('RGB', (1920, 1080), (90, 120, 180)))
    monkeypatch.setattr('PIL.ImageGrab.grab', grab)
    obj = renderer()
    result = obj.screenshot()
    assert result.size == (1280, 720)
    assert result.getpixel((640, 360)) == (90, 120, 180)
    grab.assert_called_once_with(bbox=(12, 70, 1932, 1150), all_screens=True)


@pytest.mark.parametrize('foreground,occluded', [(False, False), (True, True)])
def test_visible_fallback_refuses_other_windows(monkeypatch, foreground, occluded):
    grab = Mock()
    monkeypatch.setattr('PIL.ImageGrab.grab', grab)
    with pytest.raises(RuntimeError, match='visible'):
        renderer(foreground=foreground, occluded=occluded).screenshot()
    grab.assert_not_called()


def test_fallback_is_opt_in_and_keeps_native_nonblack_frames(monkeypatch):
    grab = Mock()
    monkeypatch.setattr('PIL.ImageGrab.grab', grab)
    assert renderer(visible=False).screenshot().getbbox() is None
    obj = renderer()
    native = Image.new('RGB', (1280, 720), (10, 20, 30))
    obj._grab.return_value = native
    assert obj.screenshot() is native
    grab.assert_not_called()


@pytest.mark.parametrize('visible,expected_origin', [(True, (8, 31)), (False, (12, 47))])
def test_visible_backend_uses_logical_client_origin_for_dpi_unaware_window(
        monkeypatch, visible, expected_origin):
    monkeypatch.setattr(
        'coding4game_gui.benchmark.windows.drivers.windows_xinput.renderer.printwindow_client_scale',
        lambda hwnd: 1.5)
    obj = renderer(visible=visible)
    obj._win32gui.GetWindowRect.return_value = (100, 100, 2044, 1239)
    obj._win32gui.ClientToScreen.return_value = (112, 147)
    obj._compute_geometry()
    x, y = expected_origin
    assert obj._crop == (x, y, x + 1280, y + 720)


def test_visible_capture_rejects_foreground_change_during_capture(monkeypatch):
    obj = renderer()
    obj._win32gui.GetForegroundWindow.side_effect = [12, 99]
    monkeypatch.setattr('PIL.ImageGrab.grab',
                        Mock(return_value=Image.new('RGB', (1920, 1080))))
    with pytest.raises(RuntimeError, match='foreground'):
        obj.screenshot()


def test_empty_client_with_only_a_window_border_uses_visible_capture(monkeypatch):
    obj = renderer()
    empty = Image.new('RGB', (1280, 720))
    empty.paste((85, 85, 85), (0, 0, 1280, 1))
    obj._grab.return_value = empty
    monkeypatch.setattr('PIL.ImageGrab.grab',
                        Mock(return_value=Image.new('RGB', (1920, 1080), (90, 120, 180))))
    assert obj.screenshot().getpixel((640, 360)) == (90, 120, 180)


def test_visible_only_capture_never_requests_a_game_redraw(monkeypatch):
    from coding4game_gui.benchmark.windows.drivers.windows_xinput import renderer as module
    obj = module.VisibleClientRenderer.__new__(module.VisibleClientRenderer)
    obj.__dict__.update(renderer().__dict__)
    obj._grab = Mock(side_effect=AssertionError('PrintWindow must not be called'))
    monkeypatch.setattr('PIL.ImageGrab.grab', Mock(
        return_value=Image.new('RGB', (1920, 1080), (90, 120, 180))))
    assert obj.screenshot().getpixel((640, 360)) == (90, 120, 180)
    obj._grab.assert_not_called()
