"""Native display preparation must not blindly toggle an existing window."""
import sys
from types import SimpleNamespace
import pytest

from coding4game_gui.benchmark.windows import startup


@pytest.mark.parametrize("process,mode,captioned,expected_keys", [
    ("TmForever.exe", "Windowed", True, 0),
    ("TmForever.exe", "Windowed", False, 4),
    ("Other.exe", "Windowed", False, 0),
    ("TmForever.exe", "Fullscreen", False, 0),
])
def test_native_toggle_is_targeted_and_confirmed(monkeypatch, process, mode, captioned, expected_keys):
    keys = []
    styles = [0xC00000 if captioned else 0]
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetWindowLong=lambda *_a: styles.pop(0) if styles else 0xC00000,
        GetWindowRect=lambda _h: (0, 0, 222, 156),
        GetClientRect=lambda _h: (0, 0, 200, 100)))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(
        WS_CAPTION=0xC00000, GWL_STYLE=-16, VK_MENU=18, VK_RETURN=13, KEYEVENTF_KEYUP=2))
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        keybd_event=lambda *args: keys.append(args)))
    from coding4game_gui.benchmark.windows import auto_nav
    monkeypatch.setattr(auto_nav, "force_foreground", lambda _h: True)
    startup._prepare_trackmania_windowed(process, mode, 7, 1)
    assert len(keys) == expected_keys
    if keys:
        assert keys == [(18, 0, 0, 0), (13, 0, 0, 0), (13, 0, 2, 0), (18, 0, 2, 0)]
