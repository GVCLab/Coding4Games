"""Mouse input driver for Windows game benchmarks.

WHY THIS EXISTS
---------------
The bench has always driven games with a virtual XInput pad, because that is
the only input channel that crosses the session-isolation wall when the
harness runs as SYSTEM via `PsExec -i 1`. Measured on 2026-07-25/26 against
Balatro, from a SYSTEM/session-1 process:

    GetCursorPos          -> works (cursor position is readable)
    SetCursorPos          -> no-op
    mouse_event clicks    -> no-op
    GetAsyncKeyState      -> never sees the user's buttons
    OpenInputDesktop      -> None
    SetWindowsHookExW     -> 0

i.e. the wall is PARTIAL: cursor position leaks out, everything else does
not. Run the very same code in the INTERACTIVE USER's session and all of it
works, mouse injection included.

That distinction matters because pad input is a bad fit for menu/card games.
Balatro's pad support is focus-based: reaching a card means walking a focus
ring (and the ring wraps DOWNWARD from the button row onto the hand, which
is not discoverable), the game silently drops inputs that arrive during its
own animations, and a mis-stepped focus turns every later press into noise.
A human just clicks the thing they want. With this driver a full round —
select cards, play, cash out, buy in the shop, next round — is a dozen
clicks at known coordinates, and each one either lands or visibly does not.

CONTRACT
--------
Coordinates are GAME-SPACE pixels, i.e. the same space the scorer's
calibrated rects live in (the renderer's output frame). For a DPI-unaware
game this is smaller than the window rect — Balatro renders 1536x864 inside
a 1920x1080 rect under 125% scaling — so callers never deal with physical
pixels or DPI at all.

Requires an interactive Windows session with access to the game window.
"""
from __future__ import annotations

import ctypes
import math
import time
from typing import Optional
from typing import Tuple

_user32 = ctypes.windll.user32

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_WHEEL_DELTA = 120


class _RelativeMouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _RelativeInputUnion(ctypes.Union):
    _fields_ = [("mi", _RelativeMouseInput)]


class _RelativeInput(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", ctypes.c_uint32), ("payload", _RelativeInputUnion)]


def _send_relative_mouse_input(event: _RelativeInput, *, send_input=None) -> int:
    """Send one mouse INPUT without sharing ctypes state with keyboard input."""
    if send_input is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send_input = user32.SendInput
        send_input.argtypes = (
            ctypes.c_uint32, ctypes.POINTER(_RelativeInput), ctypes.c_int)
        send_input.restype = ctypes.c_uint32
    return int(send_input(
        1, ctypes.byref(event), ctypes.sizeof(_RelativeInput)))


def find_game_window(process_prefix: str) -> Optional[int]:
    """Find the same non-proxy game HWND used by startup and rendering."""
    import psutil
    import win32gui
    import win32process

    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name().lower()
        except Exception:  # noqa: BLE001
            return
        if name.startswith(process_prefix.lower()) and win32gui.GetWindowText(hwnd):
            found.append({"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd)})

    win32gui.EnumWindows(_cb, None)
    from coding4game_gui.benchmark.windows.drivers.windows_xinput.lifecycle import select_game_window
    selected = select_game_window(found)
    return selected["hwnd"] if selected else None


class MouseDriver:
    """Click at game-space coordinates in the interactive user's session.

    `content_size` is the renderer's output size (what the scorer calibrated
    against). The driver rescales into the live window rect on every click,
    so a moved or resized window does not invalidate the coordinates.
    """

    def __init__(self, process_prefix: str, content_size: Tuple[int, int],
                 *, park: Tuple[int, int] = None):
        self.process_prefix = process_prefix
        self.content_size = content_size
        # Where to leave the cursor after acting. Games commonly show a
        # hover tooltip under the pointer (Balatro pops a card description
        # that covers a large slice of the board), and that tooltip WILL be
        # captured by the next screenshot and read by the scorer. Parking the
        # cursor somewhere inert keeps frames clean.
        self.park = park or (content_size[0] - 40, 40)
        self._hwnd = None

    # -- window -----------------------------------------------------------
    def hwnd(self) -> Optional[int]:
        import win32gui

        if self._hwnd and win32gui.IsWindow(self._hwnd):
            return self._hwnd
        self._hwnd = find_game_window(self.process_prefix)
        return self._hwnd

    def window_rect(self):
        import win32gui

        h = self.hwnd()
        if not h:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(h)
        return (left, top, right - left, bottom - top)

    def focus(self) -> bool:
        """Foreground the game. Unlike the SYSTEM-side path this needs no
        ALT-key trick: an interactive process is allowed to do it."""
        import win32con
        import win32gui

        h = self.hwnd()
        if not h:
            return False
        try:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(h)
        except Exception:  # noqa: BLE001
            pass
        return win32gui.GetForegroundWindow() == h

    # -- coordinates ------------------------------------------------------
    def client_on_screen(self) -> Optional[Tuple[int, int, int, int]]:
        """Client-area origin and size, in SCREEN coordinates.

        Fullscreen made the window rect and the client rect identical, so
        aiming at the window origin worked by accident. WINDOWED, the title
        bar and borders offset the client area: clicking against the window
        origin lands ~31px above the intended spot, which for a 40px button
        row is the difference between a button and whatever sits above it.
        The game draws into the CLIENT area, so that is the space the
        calibrated coordinates live in.
        """
        import win32gui

        h = self.hwnd()
        if not h:
            return None
        try:
            _, _, cw, ch = win32gui.GetClientRect(h)
            left, top = win32gui.ClientToScreen(h, (0, 0))
        except Exception:  # noqa: BLE001
            return None
        if cw <= 0 or ch <= 0:      # minimized: rect is meaningless
            return None
        return (left, top, cw, ch)

    def to_screen(self, gx: float, gy: float) -> Optional[Tuple[int, int]]:
        rect = self.client_on_screen() or self.window_rect()
        if not rect:
            return None
        left, top, width, _ = rect
        scale = width / self.content_size[0]
        return (int(left + gx * scale), int(top + gy * scale))

    def mapped_to_screen(
            self, px: float, py: float,
            coordinate_size: tuple[int, int],
    ) -> Optional[Tuple[int, int]]:
        """Map a calibrated point directly across the whole client area.

        Public pointer coordinates are first resolved into the scenario's
        calibrated space.  This second mapping uses the live client width and
        height independently.  ``to_screen`` deliberately keeps its historical
        uniform mapping for recorded menu navigation and OCR-derived points.
        """
        rect = self.client_on_screen()
        if not rect:
            return None
        left, top, width, height = rect
        pw, ph = coordinate_size
        if width <= 0 or height <= 0 or pw <= 1 or ph <= 1:
            return None
        return (
            round(left + float(px) * (width - 1) / (pw - 1)),
            round(top + float(py) * (height - 1) / (ph - 1)),
        )

    def ensure_restored(self) -> bool:
        """A minimized window reports rect (-32000, -32000) — converting a
        game coordinate against it aims the cursor off-screen, and the click
        then SUCCEEDS at the API level while doing nothing at all.

        This is not hypothetical. Balatro runs FULLSCREEN, and a fullscreen
        game minimizes itself whenever it loses focus, which anything taking
        the foreground can cause. Measured live: window 'Balatro' iconic=1,
        so all three recovery clicks were aimed at (-31940, -31898); the
        reset "ran", start_check failed, and the episode fell through to
        full_restart — which relaunches the game and thereby un-minimizes
        it, so the run recovered and the cause stayed invisible. Six such
        restarts in one lang-arm run traced back to here.
        """
        import win32gui       # deferred: this module must import on non-Windows

        h = self.hwnd()
        if not h:
            return False
        if not win32gui.IsIconic(h):
            return True
        print("[mouse] game window is minimized (a fullscreen game does this "
              "on focus loss) — restoring before clicking")
        self.focus()
        time.sleep(0.6)
        h = self.hwnd()
        return bool(h) and not win32gui.IsIconic(h)

    # -- actions ----------------------------------------------------------
    def move(self, gx: float, gy: float, *, coordinate_size=None) -> bool:
        """Aim the cursor, and VERIFY it arrived.

        This used to call SetCursorPos and return True unconditionally, which
        is how a dead input channel stayed invisible for a whole debugging
        session: SetCursorPos was returning 0 (failure) and the cursor never
        left (800, 450), yet every click reported success, so menu_skip
        "ran", start_check failed, and the run fell through to full_restart
        — over and over, with nothing in the log pointing at the mouse.

        Mouse injection needs an interactive, unlocked, attached session; it
        is exactly the sort of thing that goes away for environmental reasons
        (lock screen, disconnected session, blanked display) while capture
        and GetForegroundWindow keep working and the game looks perfectly
        healthy. Report the truth instead.
        """
        pt = (self.mapped_to_screen(gx, gy, coordinate_size)
              if coordinate_size is not None else self.to_screen(gx, gy))
        if not pt:
            return False
        if not _user32.SetCursorPos(pt[0], pt[1]):
            print(f"[mouse] SetCursorPos{pt} REFUSED — mouse injection is "
                  f"unavailable (locked / disconnected / unattached session?)")
            return False
        got = self.cursor_pos()
        if got is not None and max(abs(got[0] - pt[0]), abs(got[1] - pt[1])) > 2:
            print(f"[mouse] cursor did not move: aimed {pt}, reads {got} — "
                  f"input is being discarded or something else owns the "
                  f"pointer")
            return False
        return True

    def _move_in_space(self, gx: float, gy: float, coordinate_size) -> bool:
        """Keep the original raw-move call shape when no policy is supplied."""
        if coordinate_size is None:
            return self.move(gx, gy)
        return self.move(gx, gy, coordinate_size=coordinate_size)

    def require_game_target(self) -> None:
        """Fail before input when another window owns the cursor or focus.

        A foreground game can still be covered by a topmost overlay.  Sending
        a click in that state both misses the game and can mutate an unrelated
        application, so public pointer actions check both ownership signals at
        the final boundary.  Recorded startup navigation deliberately does not
        opt into this guard.
        """
        import win32gui
        import win32process

        game = self.hwnd()
        cursor = self.cursor_pos()
        if not game or cursor is None:
            raise RuntimeError(
                "pointer target check failed: game window or cursor unavailable")
        hit = win32gui.WindowFromPoint(cursor)
        foreground = win32gui.GetForegroundWindow()

        def root(hwnd):
            return win32gui.GetAncestor(hwnd, 2) if hwnd else 0  # GA_ROOT

        def belongs(hwnd):
            return bool(hwnd and (
                hwnd == game or win32gui.IsChild(game, hwnd) or root(hwnd) == game))

        if belongs(hit) and belongs(foreground):
            return

        def identity(hwnd):
            if not hwnd:
                return "hwnd=0 pid=0 class=''"
            try:
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            except Exception:  # noqa: BLE001 - diagnostics must not mask guard
                pid = 0
            try:
                class_name = win32gui.GetClassName(hwnd)
            except Exception:  # noqa: BLE001
                class_name = ""
            return f"hwnd={int(hwnd)} pid={int(pid)} class={class_name!r}"

        raise RuntimeError(
            "pointer target is not the foreground game window: "
            f"game({identity(game)}), hit({identity(hit)}), "
            f"foreground({identity(foreground)}), cursor={cursor}")

    def relative_move(self, dx: int, dy: int) -> bool:
        """Inject one relative mouse event for a captured game camera.

        Unlike :meth:`move`, the values are signed device deltas rather than
        game-space coordinates.  API acceptance proves delivery only; live
        acceptance must additionally observe the game's camera response.
        """
        if not self.ensure_restored():
            return False
        event = _RelativeInput(type=0)
        event.mi = _RelativeMouseInput(
            dx=int(dx), dy=int(dy), mouseData=0,
            dwFlags=_MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0)
        sent = _send_relative_mouse_input(event)
        if sent != 1:
            print(
                f"[mouse] relative move ({dx}, {dy}) REFUSED; "
                f"SendInput inserted {sent} event(s)")
            return False
        return True

    @staticmethod
    def cursor_pos():
        """Where the pointer actually is, or None if it cannot be read."""
        import ctypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        p = _PT()
        if not _user32.GetCursorPos(ctypes.byref(p)):
            return None
        return (p.x, p.y)

    def click(self, gx: float, gy: float, *, settle: float = 0.45,
              park: bool = False, move_settle: float = 0.12,
              button_hold: float = 0.05, button: str = "left",
              count: int = 1, inter_click: float = 0.06,
              coordinate_size=None, require_game_target: bool = False) -> bool:
        """Click at a game-space coordinate with explicit button/count.

        `settle` gives the UI time to react; games that animate their menus
        drop inputs that arrive mid-transition, so back-to-back clicks with
        no gap are unreliable even though each individual click is correct.
        Navigation keeps the conservative defaults. Gameplay policy clicks
        pass short move/button timings and use their ACT tics as the complete
        settle window instead of paying this fixed delay as hidden game time.
        """
        if not self.ensure_restored():
            return False        # loud: _click_game_points raises on False
        if not self._move_in_space(gx, gy, coordinate_size):
            return False
        if move_settle > 0:
            time.sleep(float(move_settle))
        try:
            down, up = {
                "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
                "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
                "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
            }[str(button).lower()]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        if count not in (1, 2):
            raise ValueError("mouse click count must be 1 or 2")
        for index in range(count):
            if require_game_target:
                self.require_game_target()
            _user32.mouse_event(down, 0, 0, 0, 0)
            try:
                if button_hold > 0:
                    time.sleep(float(button_hold))
            finally:
                _user32.mouse_event(up, 0, 0, 0, 0)
            if index + 1 < count and inter_click > 0:
                time.sleep(float(inter_click))
        if settle > 0:
            time.sleep(float(settle))
        if park:
            self.park_cursor()
        return True

    def click_current(self, *, button_hold: float = 0.05,
                      require_game_target: bool = False) -> bool:
        """Left-click at the captured pointer position without moving it."""
        if not self.ensure_restored():
            return False
        if require_game_target:
            self.require_game_target()
        _user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        try:
            if button_hold > 0:
                time.sleep(button_hold)
        finally:
            _user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True

    def right_click(self, gx: float, gy: float, **kwargs) -> bool:
        """Right-click without exposing a cross-action held-button state."""
        return self.click(gx, gy, button="right", **kwargs)

    def middle_click(self, gx: float, gy: float, **kwargs) -> bool:
        """Middle-click without exposing a cross-action held-button state."""
        return self.click(gx, gy, button="middle", **kwargs)

    def double_click(self, gx: float, gy: float, **kwargs) -> bool:
        """Issue a deterministic two-click sequence at one coordinate."""
        return self.click(gx, gy, button="left", count=2, **kwargs)

    def drag(self, start: tuple[float, float], end: tuple[float, float],
             *, duration: float, steps_per_second: int = 60,
             coordinate_size=None, require_game_target: bool = False) -> bool:
        """Perform one complete straight-line left drag and always release.

        ``duration`` covers aiming at the start, button down, interpolation,
        and button up. If delivery overruns, the benchmark layer charges the
        measured live duration rather than hiding the extra game time.
        """
        if not self.ensure_restored():
            return False
        started = time.perf_counter()
        if not self._move_in_space(*start, coordinate_size):
            return False
        if require_game_target:
            self.require_game_target()
        _user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        ok = True
        try:
            duration = max(0.0, float(duration))
            steps = max(1, int(math.ceil(duration * steps_per_second)))
            sx, sy = start
            ex, ey = end
            for index in range(1, steps + 1):
                target_time = started + duration * index / steps
                remaining = target_time - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
                ratio = index / steps
                if not self._move_in_space(
                    sx + (ex - sx) * ratio,
                    sy + (ey - sy) * ratio,
                    coordinate_size,
                ):
                    ok = False
                    break
        finally:
            _user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return ok

    def scroll(self, gx: float, gy: float, *, dx: int, dy: int,
               coordinate_size=None, require_game_target: bool = False) -> bool:
        """Scroll at one game-space point; positive X is right, Y is up."""
        if (not self.ensure_restored() or
                not self._move_in_space(gx, gy, coordinate_size)):
            return False
        if require_game_target:
            self.require_game_target()
        if dx:
            _user32.mouse_event(
                _MOUSEEVENTF_HWHEEL, 0, 0,
                ctypes.c_ulong(int(dx) * _WHEEL_DELTA).value, 0)
        if dy:
            _user32.mouse_event(
                _MOUSEEVENTF_WHEEL, 0, 0,
                ctypes.c_ulong(int(dy) * _WHEEL_DELTA).value, 0)
        return True

    def park_cursor(self) -> None:
        """Move the pointer somewhere inert so hover tooltips do not cover
        the board in the next captured frame."""
        self.move(*self.park)
        time.sleep(0.15)
