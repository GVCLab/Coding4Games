"""Windows screenshot backends.

Capture backends share the same `Renderer` interface:
  - PrintWindow:   HWND-based; works with covered / off-screen windows
                   but fails on DX12 / Vulkan swapchains.
  - dxcam:         DXGI desktop duplication; captures the framebuffer
                   directly; required for modern engines.
  - pyautogui:     screen region capture; fallback.
  - PrintWindow visible: opt-in visible client recovery for omitted surfaces.
  - visible_client: visible client capture without PrintWindow render requests.

`auto_detect_screenshot_backend()` picks one by inspecting the target
process for DX12 / Vulkan markers.
"""

from __future__ import annotations

from typing import Optional

import psutil
from PIL import Image

import ctypes

try:
    from ctypes import windll  # Windows-only (advapi32 ctypes proxy)
except Exception:  # noqa: BLE001
    windll = None  # type: ignore


# Engines / APIs that PrintWindow can't capture (DX12, Vulkan, recent DXGI
# flip models). When we see any of these DLLs loaded in the target
# process, prefer dxcam over PrintWindow.
_DX12_VULKAN_DLLS = frozenset({
    "d3d12.dll",
    "d3d12core.dll",
    "vulkan-1.dll",
    "amdvlk64.dll",
    "amdxc64.dll",
})


def _process_has_dll(pid: int, dll_basenames: frozenset) -> Optional[str]:
    try:
        proc = psutil.Process(pid)
        for m in proc.memory_maps():
            base = m.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
            if base in dll_basenames:
                return base
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return None
    return None


def auto_detect_screenshot_backend(pid: int) -> str:
    """Heuristic: DX12 / Vulkan loaded → dxcam. Otherwise printwindow."""
    matched = _process_has_dll(pid, _DX12_VULKAN_DLLS)
    if matched:
        print(f"Auto-detect: process has '{matched}' loaded → using dxcam")
        return "dxcam"
    print("Auto-detect: no DX12/Vulkan markers found → using printwindow")
    return "printwindow"


class PyautoguiRenderer:
    name = "pyautogui"

    def __init__(self, bbox):
        import pyautogui
        self._pyautogui = pyautogui
        self.bbox = bbox
        self._geom_key = None

    def screenshot(self):
        return self._pyautogui.screenshot(region=self.bbox)

    def close(self) -> None:
        pass


def _dxcam_output_region(
    bbox: tuple[int, int, int, int],
    monitor_rects: list[tuple[int, int, int, int]],
) -> tuple[int, tuple[int, int, int, int]]:
    """Map a virtual-desktop client bbox to one dxcam output's coordinates.

    Win32 reports window positions in virtual-desktop coordinates, where a
    monitor left of the primary display has a negative X.  dxcam regions are
    instead local to the selected output.  Passing the virtual coordinates to
    dxcam's default (primary) output makes secondary-monitor captures fail as
    an invalid region.
    """
    left, top, width, height = bbox
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid window bbox {bbox!r}")
    right, bottom = left + width, top + height
    for output_idx, (mon_left, mon_top, mon_right, mon_bottom) in enumerate(
            monitor_rects):
        if (mon_left <= left and top >= mon_top
                and right <= mon_right and bottom <= mon_bottom):
            return output_idx, (
                left - mon_left,
                top - mon_top,
                right - mon_left,
                bottom - mon_top,
            )
    raise ValueError(
        f"window bbox {bbox!r} must be fully inside one monitor; "
        f"monitors={monitor_rects!r}"
    )


class DxcamRenderer:
    name = "dxcam"

    def __init__(self, bbox, fps: int, *, hwnd=None, pid=None):
        import dxcam
        import win32api

        self.bbox = bbox
        self.hwnd, self.pid = hwnd, pid
        scale = printwindow_client_scale(hwnd) if hwnd is not None else 1.0
        self.frame_size = (round(bbox[2] / scale), round(bbox[3] / scale))
        monitor_rects = [
            tuple(rect)
            for _monitor, _hdc, rect in win32api.EnumDisplayMonitors()
        ]
        self.output_idx, region = _dxcam_output_region(bbox, monitor_rects)
        print(f"dxcam: output={self.output_idx} region={region} "
              f"virtual_bbox={bbox}")
        self.camera = dxcam.create(output_idx=self.output_idx)
        self.last = None
        self.camera.start(region=region,
                          target_fps=fps, video_mode=True)

    def _check_surface(self):
        if self.hwnd is None:
            return
        import win32gui as g
        import win32process
        if not g.IsWindow(self.hwnd) or g.IsIconic(self.hwnd):
            raise RuntimeError('desktop capture requires a non-minimized game window')
        if (self.pid is not None
                and win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid):
            raise RuntimeError('desktop capture HWND belongs to another process')
        if g.GetForegroundWindow() != self.hwnd:
            raise RuntimeError('desktop capture requires the game foreground')
        left, top, right, bottom = g.GetClientRect(self.hwnd)
        x, y = g.ClientToScreen(self.hwnd, (left, top))
        width, height = right - left, bottom - top
        if (x, y, width, height) != tuple(self.bbox):
            raise RuntimeError('desktop capture client geometry changed')
        for px in (x, x + width // 2, x + width - 1):
            for py in (y, y + height // 2, y + height - 1):
                root = g.GetAncestor(g.WindowFromPoint((px, py)), 2)
                if root != self.hwnd:
                    _, owner = win32process.GetWindowThreadProcessId(root)
                    obstruction_class = g.GetClassName(root)
                    self._record_owned_obstruction(root, owner, (px, py))
                    raise RuntimeError(
                        f'desktop capture client is obscured at {(px, py)}: '
                        f'root={root:#x}, class={obstruction_class!r}, '
                        f'pid={owner}, game={self.hwnd:#x}')

    def _record_owned_obstruction(self, root, owner, point):
        """Optionally preserve failed-capture evidence without accepting a frame."""
        import os
        destination = os.environ.get('C4G_CAPTURE_ERROR_EVIDENCE_DIR')
        if (not destination or self.pid is None or owner != self.pid
                or getattr(self, '_obstruction_evidence_attempted', False)):
            return
        self._obstruction_evidence_attempted = True
        # Diagnostic collection must never replace the original capture error.
        try:
            import json
            from pathlib import Path
            import time
            import win32gui as g
            import win32process
            from PIL import ImageGrab

            directory = Path(destination)
            if not directory.is_absolute():
                return
            if (g.GetForegroundWindow() != self.hwnd
                    or win32process.GetWindowThreadProcessId(root)[1] != self.pid):
                return
            left, top, width, height = self.bbox
            box = (left, top, left + width, top + height)
            record = {
                'unix': time.time(), 'game_hwnd': self.hwnd, 'game_pid': self.pid,
                'obstruction_root': root, 'obstruction_class': g.GetClassName(root),
                'obstruction_rect': list(g.GetWindowRect(root)),
                'sample_point': list(point), 'client_box': list(box),
                'source': 'independent desktop capture after a rejected surface check',
                'accepted_observation': False,
            }
            pixels = ImageGrab.grab(bbox=box, all_screens=True)
            after = g.GetAncestor(g.WindowFromPoint(point), 2)
            record['same_root_after_capture'] = after == root
            record['game_foreground_after_capture'] = g.GetForegroundWindow() == self.hwnd
            directory.mkdir(parents=True, exist_ok=True)
            stem = f'{self.pid}_{time.time_ns()}'
            pixels.save(directory / f'{stem}.png')
            record['image'] = f'{stem}.png'
            (directory / f'{stem}.json').write_text(
                json.dumps(record, indent=2) + '\n', encoding='utf-8')
        except Exception:
            pass

    def screenshot(self):
        self._check_surface()
        frame = self.camera.get_latest_frame()
        self._check_surface()
        if frame is None:
            raise RuntimeError('desktop capture returned no frame')
        img = Image.fromarray(frame)
        if img.size != tuple(self.bbox[2:]):
            raise RuntimeError('desktop capture frame dimensions differ from the client')
        if img.size != self.frame_size:
            # Reverse only the measured Windows presentation stretch.
            img = img.resize(self.frame_size, Image.Resampling.LANCZOS)
        self.last = img
        return img

    def close(self) -> None:
        if self.camera is not None:
            # DXcam caches cameras by output. Stop alone leaves that instance
            # available to the next game; release invalidates it in the factory.
            self.camera.release()
            self.camera = None
            self.last = None


def printwindow_client_scale(hwnd) -> float:
    """Physical client pixels per captured pixel for a DPI-unaware game."""
    try:
        user = windll.user32
        user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        context = user.GetWindowDpiAwarenessContext(hwnd)
        if user.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(context)) == 0:
            dpi = user.GetDpiForSystem()
            if dpi:
                return dpi / 96.0
    except Exception:
        pass
    return 1.0


class PrintWindowRenderer:
    """HWND capture, with optional foreground-only visible client recovery."""

    name = "printwindow"
    _PW_RENDERFULLCONTENT = 2

    def __init__(self, hwnd, bbox, pid: Optional[int] = None,
                 visible_fallback: bool = False):
        import win32gui
        import win32ui
        self._win32gui = win32gui
        self._win32ui = win32ui
        self._printwindow = windll.user32.PrintWindow
        self.hwnd = hwnd
        self.pid = pid
        self.bbox = bbox
        self.visible_fallback = visible_fallback
        if visible_fallback:
            self.name = "printwindow_visible"
        # PrintWindow renders the WHOLE window (incl. title bar + borders) into
        # a DC. We size the bitmap to the full window, then crop to the client
        # area so the agent sees only the game viewport — matching the client
        # bbox the screen-region backends (dxcam/pyautogui) already use.
        self._compute_geometry()

    def _compute_geometry(self) -> None:
        """Cache the full-window size + the client area's offset within it.

        Window position is stable during a run (the runner force-foregrounds
        but never moves/resizes it), so computing once at init is enough.
        """
        g = self._win32gui
        wl, wt, wr, wb = g.GetWindowRect(self.hwnd)
        self.width = int(wr - wl)   # bitmap size = full window
        self.height = int(wb - wt)
        self._geom_key = self._geometry_key()

        # A DPI-UNAWARE game renders at LOGICAL size and lets Windows stretch
        # the result for display, but PrintWindow captures the UNSTRETCHED
        # surface -- so a bitmap sized in PHYSICAL pixels comes back with
        # black padding on the right/bottom.
        #
        # Whether we SEE physical or logical pixels depends on OUR process,
        # not the game's: a DPI-unaware reader gets GetClientRect virtualised
        # (divided by the scale) and needs no correction, while a DPI-AWARE
        # one gets physical pixels and does. This bench is DPI-aware in
        # practice — the OCR stack flips it — and the two cases were measured
        # differing by exactly the scale factor: 1228x691 read from a plain
        # probe, 1535x863 for the same window from the bench (x1.25).
        #
        # BOTH corrections can be needed at once, and that is the case this
        # code used to get wrong: windowed puts the client area at an offset
        # inside the window AND the captured surface is logical-sized. The
        # client branch returned before the DPI branch could run, so the
        # frame kept its padding, came back at the calibrated size by
        # coincidence, and every scorer rect silently read the wrong pixels.
        scale = printwindow_client_scale(self.hwnd)

        left = top = 0
        cw, ch = self.width, self.height
        try:
            cl, ct, cr, cb = g.GetClientRect(self.hwnd)
            sx, sy = g.ClientToScreen(self.hwnd, (cl, ct))
            if cr - cl > 0 and cb - ct > 0:
                left, top = sx - wl, sy - wt
                cw, ch = cr - cl, cb - ct
        except Exception:  # noqa: BLE001
            pass

        # The game drew `cw/scale x ch/scale` logical pixels at the client
        # origin; everything beyond that is padding.
        rw = int(round(cw / scale))
        rh = int(round(ch / scale))
        if getattr(self, "visible_fallback", False) and scale != 1.0:
            # The full PrintWindow surface includes logical-size decorations.
            # Use the same logical client origin as its unscaled game pixels,
            # so native and visible captures cover the identical viewport.
            # Retain the legacy backend's crop for existing calibrations.
            left = int(round(left / scale))
            top = int(round(top / scale))
        rw = max(1, min(rw, self.width - left))
        rh = max(1, min(rh, self.height - top))
        if (left, top, rw, rh) == (0, 0, self.width, self.height):
            self._crop = None     # client == window and no scaling: nothing to do
            return
        if scale != 1.0:
            print(f'[renderer] DPI-unaware game at {scale:g}x: cropping capture '
                  f'{self.width}x{self.height} -> {rw}x{rh} at ({left},{top})')
        self._crop = (left, top, left + rw, top + rh)

    def _refind_hwnd(self) -> bool:
        """The game can destroy and recreate its window (Steam relaunch /
        Godot mode switch — observed live on the full_restart chain
        2026-07-09): the cached hwnd then throws error 1400 on every
        capture. Re-resolve the process's largest visible top-level window
        and recompute geometry."""
        if not self.pid:
            return False
        import win32process
        g = self._win32gui
        cands: list[tuple[int, int]] = []

        def _cb(h, _):
            if not g.IsWindowVisible(h):
                return
            _tid, p = win32process.GetWindowThreadProcessId(h)
            if p != self.pid:
                return
            left, top, right, bottom = g.GetWindowRect(h)
            cands.append((max(0, right - left) * max(0, bottom - top), h))

        g.EnumWindows(_cb, None)
        if not cands:
            return False
        new = max(cands)[1]
        print(f"printwindow: window handle changed "
              f"{self.hwnd:#x} -> {new:#x} (pid {self.pid})")
        self.hwnd = new
        self._compute_geometry()
        return True

    def screenshot(self):
        if not self._win32gui.IsWindow(self.hwnd) and not self._refind_hwnd():
            raise RuntimeError(
                f"printwindow: window for pid {self.pid} is gone")
        self._refresh_geometry_if_resized()
        try:
            frame = self._grab()
        except Exception:  # noqa: BLE001 — stale handle: refind once, retry
            if not self._refind_hwnd():
                raise
            frame = self._grab()
        if getattr(self, "visible_fallback", False):
            width, height = frame.size
            # DPI rounding can retain a one-pixel non-client border even when
            # PrintWindow omitted the entire game surface. Inspect the interior
            # for that failure; successful output keeps the complete viewport.
            interior = (frame.crop((1, 1, width - 1, height - 1))
                        if width > 2 and height > 2 else frame)
            if interior.getbbox() is None:
                return self._visible_client(frame.size)
        return frame

    def _visible_client(self, native_size):
        """Recover a visible client frame, undoing only Windows DPI stretching."""
        from PIL import ImageGrab
        g = self._win32gui

        def visible_rect():
            if g.GetForegroundWindow() != self.hwnd:
                raise RuntimeError("visible capture requires the game foreground")
            left, top, right, bottom = g.GetClientRect(self.hwnd)
            x, y = g.ClientToScreen(self.hwnd, (left, top))
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                raise RuntimeError("visible capture has no client area")
            points = ((x, y), (x + width - 1, y),
                      (x, y + height - 1), (x + width - 1, y + height - 1),
                      (x + width // 2, y + height // 2))
            for point in points:
                root = g.GetAncestor(g.WindowFromPoint(point), 2)
                if root != self.hwnd:
                    raise RuntimeError(
                        f"visible capture client is obscured at {point}: "
                        f"root={root:#x}, game={self.hwnd:#x}, "
                        f"client={(x, y, width, height)}")
            return x, y, x + width, y + height

        rect = visible_rect()
        frame = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")
        if visible_rect() != rect:
            raise RuntimeError("visible capture client moved during capture")
        # PrintWindow emits the unscaled game surface. Desktop capture sees
        # Windows' presentation stretch; reverse that measured geometry so
        # changing the capture path does not change the observation coordinates.
        if frame.size != native_size:
            frame = frame.resize(native_size, Image.Resampling.LANCZOS)
        return frame

    def _geometry_key(self):
        """The raw measurements the crop is derived from."""
        g = self._win32gui
        try:
            return (g.GetWindowRect(self.hwnd), g.GetClientRect(self.hwnd))
        except Exception:  # noqa: BLE001
            return None

    def _refresh_geometry_if_resized(self) -> None:
        """Recompute the crop when the window it was measured from changes.

        Geometry was computed ONCE at init, on the documented assumption that
        the runner never moves or resizes the window. A scenario that declares
        its own geometry breaks that directly: the game is launched, briefly
        occupies one size, and settles into another, so a renderer built
        during the transient keeps sizing its bitmap to a window that no
        longer exists — the frame then comes back at the calibrated size by
        coincidence, _conform does nothing, and every scorer rect silently
        reads the wrong pixels.

        Compare the INPUTS, not the output size: the emitted frame is in the
        game's LOGICAL pixels while the client rect is in physical ones, so
        "frame == client" is false by design whenever a DPI-unaware game is
        read by a DPI-aware process — a first attempt at this check compared
        those two and recomputed the geometry on every single screenshot.
        """
        key = self._geometry_key()
        if key is None or key == self._geom_key:
            return
        if key[1][2] - key[1][0] <= 0 or key[1][3] - key[1][1] <= 0:
            return                      # minimized: nothing meaningful to read
        print(f"[renderer] window geometry changed {self._geom_key} -> {key}"
              f" — recomputing capture geometry")
        self._compute_geometry()

    def _grab(self):
        # Every GDI handle is acquired INSIDE the try so a mid-sequence failure
        # (e.g. CreateCompatibleBitmap under GDI-pool pressure) still frees what
        # was already allocated — a leak here accelerates the very handle
        # exhaustion that wedges the session (black screen / BitBlt failed).
        g, u = self._win32gui, self._win32ui
        hwnd_dc = mfc_dc = save_dc = bmp = None
        try:
            hwnd_dc = g.GetWindowDC(self.hwnd)
            mfc_dc = u.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = u.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, self.width, self.height)
            save_dc.SelectObject(bmp)
            self._printwindow(self.hwnd, save_dc.GetSafeHdc(), self._PW_RENDERFULLCONTENT)
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            img = Image.frombuffer(
                "RGB", (info["bmWidth"], info["bmHeight"]),
                bits, "raw", "BGRX", 0, 1,
            )
        finally:
            if bmp is not None:
                try:
                    g.DeleteObject(bmp.GetHandle())
                except Exception:  # noqa: BLE001
                    pass
            if save_dc is not None:
                try:
                    save_dc.DeleteDC()
                except Exception:  # noqa: BLE001
                    pass
            if mfc_dc is not None:
                try:
                    mfc_dc.DeleteDC()
                except Exception:  # noqa: BLE001
                    pass
            if hwnd_dc is not None:
                try:
                    g.ReleaseDC(self.hwnd, hwnd_dc)
                except Exception:  # noqa: BLE001
                    pass
        if self._crop is not None:
            img = img.crop(self._crop)
        return img

    def close(self) -> None:
        pass


class VisibleClientRenderer(PrintWindowRenderer):
    """Read the visible client without sending render requests to the game."""

    name = "visible_client"

    def __init__(self, hwnd, bbox, pid: Optional[int] = None):
        import win32gui
        self._win32gui = win32gui
        self.hwnd = hwnd
        self.pid = pid
        self.bbox = bbox
        self.visible_fallback = True
        self._compute_geometry()

    def screenshot(self):
        if not self._win32gui.IsWindow(self.hwnd) and not self._refind_hwnd():
            raise RuntimeError(f"visible capture window for pid {self.pid} is gone")
        self._refresh_geometry_if_resized()
        size = ((self._crop[2] - self._crop[0], self._crop[3] - self._crop[1])
                if self._crop else (self.width, self.height))
        return self._visible_client(size)


def make_windows_renderer(
    *, hwnd: int, bbox: tuple[int, int, int, int],
    pid: int, env_fps: int, backend: Optional[str],
):
    """Pick + construct a renderer. `backend` may be 'auto' / None / a name."""
    if backend in (None, "auto"):
        backend = auto_detect_screenshot_backend(pid)

    if backend == "dxcam":
        try:
            return DxcamRenderer(bbox, env_fps, hwnd=hwnd, pid=pid)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: dxcam init failed ({exc!r}); falling back to pyautogui")
            return PyautoguiRenderer(bbox)
    if backend == "pyautogui":
        return PyautoguiRenderer(bbox)
    if backend == "printwindow":
        return PrintWindowRenderer(hwnd, bbox, pid=pid)
    if backend == "printwindow_visible":
        return PrintWindowRenderer(hwnd, bbox, pid=pid, visible_fallback=True)
    if backend == "visible_client":
        return VisibleClientRenderer(hwnd, bbox, pid=pid)
    raise ValueError(
        f"unknown screenshot_backend {backend!r} "
        f"(expected: printwindow, printwindow_visible, visible_client, dxcam, pyautogui, auto)"
    )
