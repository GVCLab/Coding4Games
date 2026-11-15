"""Handle narrowly identified optional offers before the game window exists."""

from __future__ import annotations

import win32con
import win32gui

from coding4game_gui.benchmark.evidence.event_log import emit


def is_nvidia_driver_download_offer(text: str) -> bool:
    compact = "".join(text.split())
    return (
        "NVIDIA" in compact
        and "https://www.nvidia.com/en-us/geforce/drivers/" in compact
        and "是否要访问以下URL下载驱动" in compact
        and ("D3D11" in compact or "D3D12" in compact)
    )


def decline_palworld_driver_download(process_name: str, hwnd: int) -> bool:
    """Decline the observed download-webpage question, never an install prompt."""
    if process_name.casefold() != "palworld-win64-shipping.exe":
        return False
    if win32gui.GetClassName(hwnd) != "#32770":
        return False
    texts = []
    win32gui.EnumChildWindows(
        hwnd, lambda child, _data: texts.append(win32gui.GetWindowText(child)), None)
    if not is_nvidia_driver_download_offer(" ".join(texts)):
        return False
    button = win32gui.GetDlgItem(hwnd, win32con.IDNO)
    if not button or not win32gui.GetWindowText(button).replace("&", "").startswith("否("):
        raise RuntimeError("Recognized driver download offer has no verified No button")
    # This invokes the native dialog's No button. It does not acknowledge an
    # installation, suppress future driver checks, or call into the game engine.
    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, win32con.IDNO, button)
    emit("startup_modal", "declined_download", process=process_name,
         reason="nvidia_driver_download_offer", choice="no")
    print("[prelaunch] Declined the optional NVIDIA driver download webpage", flush=True)
    return True
