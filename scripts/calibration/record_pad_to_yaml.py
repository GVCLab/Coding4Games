"""Record a mouse-clicked demo into games/<game>.yaml as PadActionSpec steps.

Workflow:
  1. Launch the target game (manually OR pass --auto-launch and the
     recorder will run the game's auto_launch.launch_path, wait for its
     window, then open the recorder window).
  2. Use the recorder window to drive the selected virtual Xbox gamepads.
  3. Click buttons to demo a sequence. Vgamepad drives the real game in
     real time — you SEE menus respond.
  4. Mouse modes:
       - LEFT-click a button   = press/hold (release on mouseup)
       - RIGHT-click a button  = toggle hold (stays pressed until right-clicked again)
       - RELEASE ALL button    = drops every held button immediately
       - drag MOVE WINDOW      = reposition the recorder without generating input
  5. SAVE = stop + write YAML. CANCEL = exit without saving. CHECKPOINT
     = tag the NEXT recorded step's description with "## CHECKPOINT:".
     Ctrl+Alt+S / Ctrl+Alt+Q / Ctrl+Alt+C work while the game owns foreground.

Input isolation: the game stays foreground (it only polls XInput there), so
physical CLICKS and keystrokes are consumed by low-level hooks — otherwise the
same click that presses a virtual pad button also reaches the game.  Mouse
MOVEMENT is passed through, so there is exactly one real cursor.  While the
hooks are live the only keys that work are Ctrl+Alt+S/Q/C; an on-screen banner
says so, and a watchdog drops the hooks if the UI thread ever wedges (otherwise
a hang would leave the operator with no working keyboard or mouse).
"""

from __future__ import annotations

import argparse
import ctypes
import io
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from coding4game_gui.benchmark.windows.menu_recorder import PadStateTracker
from coding4game_gui.benchmark.windows.menu_recorder import YamlPatcher
from coding4game_gui.benchmark.windows.games_config import resolve_game_path
from coding4game_gui.benchmark.windows.gamepad import resolve_button
from coding4game_gui.benchmark.windows.drivers.windows_xinput.input import XInputDriver


# Win32 messages used by the recorder's physical-input isolation layer.  The
# game deliberately remains foreground so it keeps polling XInput; therefore
# ordinary Tk mouse bindings are not enough: the same hardware click can also
# reach the game as an attack.  A low-level hook consumes the hardware event
# and the recorder dispatches it to its own controls.
#
# Mouse MOVEMENT is deliberately NOT consumed.  Swallowing it froze the OS
# cursor (a hook returning non-zero means the move never reaches the system,
# so the pointer stops updating) while ShowCursor could not actually hide it —
# its display counter is per-input-queue and the foreground game re-asserts its
# own cursor via WM_SETCURSOR.  The recorder then had to paint a fake pointer
# and re-derive position from accumulated deltas, which is why the operator saw
# TWO cursors.  Games driven by this recorder are read via XInput only, so
# letting real movement through costs nothing and keeps one real cursor.
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002


def _should_swallow_physical_mouse(message: int, *, flags: int) -> bool:
    """Return True for real mouse traffic that must not reach the game."""
    if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
        return False
    return message in {
        WM_LBUTTONDOWN,
        WM_LBUTTONUP,
        WM_RBUTTONDOWN,
        WM_RBUTTONUP,
        WM_MOUSEWHEEL,
    }


def _should_deliver_to_recorder(message: int, *, flags: int) -> bool:
    """Return True for mouse traffic the recorder's own dispatcher must see.

    Wider than `_should_swallow_physical_mouse` by exactly WM_MOUSEMOVE: moves
    pass through to the OS (so there is one real, moving cursor) but the
    recorder still needs them for left-stick dragging.  It cannot fall back to
    Tk's own <B1-Motion> for that, because the ButtonPress that would start the
    drag IS swallowed — Tk never sees button 1 go down, so it never synthesizes
    motion-while-held.
    """
    if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
        return False
    return message == WM_MOUSEMOVE or _should_swallow_physical_mouse(
        message, flags=flags,
    )


# --------------------- pre-flight ---------------------

def preflight_check_os() -> None:
    if sys.platform != "win32":
        sys.exit("ERROR: recorder is Windows-only (vgamepad / Tk Win32 key events).")


def preflight_check_vgamepad_importable() -> None:
    try:
        import vgamepad  # noqa: F401
    except Exception as e:
        sys.exit(
            f"ERROR: vgamepad import failed: {e}\n"
            f"  Install ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases"
        )


# --------------------- Tkinter capture window ---------------------

# Event queue protocol: every entry is a dict.
#   {'type': 'pad', 't': float, 'pad': 1|2, 'kind': str, 'token': str, 'action': 'down'|'up'}
#   {'type': 'control', 't': float, 'token': 'stop_save'|'cancel'|'checkpoint'|'release_all'}
#   {'type': 'system', 't': float, 'token': 'shutdown'|'closed'}

# Visual language: a compact Xbox-style calibration deck. The shell is cool
# graphite; input feedback is deliberately distinct from the ABXY colors.
COLOR_BG_PANEL = "#0f1319"
COLOR_BG_SURFACE = "#171d25"
COLOR_SHELL = "#29313c"
COLOR_SHELL_EDGE = "#596779"
COLOR_SHELL_INSET = "#1b222b"
COLOR_BG_BTN = "#364250"
COLOR_FG_BTN = "#f3f6fa"
COLOR_MUTED = "#9ba8b8"
COLOR_TAP_HELD = "#f2bd4b"       # amber during a left-click tap
COLOR_TOGGLE_HELD = "#28c7bd"    # cyan when right-click-toggled

FACE_BUTTON_COLORS = {
    "A": "#32a852",
    "B": "#d94b4b",
    "X": "#357fc4",
    "Y": "#d8a928",
}

# Normalized positions inside the controller canvas. The asymmetry is
# intentional: it mirrors an Xbox controller instead of a generic button grid.
PAD_CONTROL_LAYOUT: dict[str, tuple[float, float, str, str]] = {
    "LT": (0.18, 0.10, "LT\nLEFT TRIGGER", "wide"),
    "RT": (0.82, 0.10, "RT\nRIGHT TRIGGER", "wide"),
    "LB": (0.28, 0.19, "LB", "shoulder"),
    "RB": (0.72, 0.19, "RB", "shoulder"),
    "Y": (0.76, 0.34, "Y", "face"),
    "X": (0.67, 0.44, "X", "face"),
    "B": (0.85, 0.44, "B", "face"),
    "A": (0.76, 0.54, "A", "face"),
    "BACK": (0.44, 0.35, "VIEW\nBACK", "system"),
    "START": (0.56, 0.35, "MENU\nSTART", "system"),
    "DPAD_UP": (0.39, 0.61, "D \u2191", "dpad"),
    "DPAD_LEFT": (0.31, 0.70, "D \u2190", "dpad"),
    "DPAD_RIGHT": (0.47, 0.70, "D \u2192", "dpad"),
    "DPAD_DOWN": (0.39, 0.79, "D \u2193", "dpad"),
}

# Unlike the D-pad, the left stick is one continuous two-axis control.  Its
# draggable cap occupies this normalized controller position.
LEFT_STICK_LAYOUT = (0.25, 0.44)

PAD_CONTROL_SIZES = {
    "wide": (124, 44),
    "shoulder": (76, 34),
    "face": (46, 46),
    "system": (60, 34),
    "dpad": (50, 34),
}

# Debug flags — toggled by CLI `--debug` (sets both)
DEBUG_UI: bool = False
DEBUG_WORKER: bool = False

# Anti-double-click: a press on the same button within this window of the
# previous press on that button is treated as an accidental bounce and dropped.
# Tunable via `--debounce-ms`; set to 0 to disable.
DEBOUNCE_S: float = 0.12

# Shown over the game for the whole recording. Every chord that still works
# must appear here: with the isolation hooks installed nothing else does, and an
# operator who cannot see this cannot tell a live recorder from a frozen box.
ESCAPE_BANNER_TEXT = (
    "⌨ PHYSICAL KEYBOARD / MOUSE ARE BLOCKED WHILE RECORDING     "
    "Ctrl+Alt+S = SAVE     Ctrl+Alt+Q = CANCEL & QUIT     "
    "Ctrl+Alt+C = CHECKPOINT     ·     stuck? Ctrl+Alt+Del → kill python.exe"
)


def _make_click_through(widget) -> None:
    """Mark a Toplevel as an overlay: never activated, never hit-tested, and
    absent from Alt-Tab. Used by the virtual cursor and the escape banner —
    both must float over the game without ever taking focus from it."""
    if sys.platform != "win32":
        return
    try:
        hwnd = widget.winfo_id()
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
    except Exception:
        pass


class PadControl(tk.Canvas):
    """Canvas-backed control that can look circular or pill-shaped.

    It accepts ``configure(bg=...)`` so the existing recorder state machine
    can paint it. Returning to the neutral color restores each control's own
    hardware color, including the standard ABXY colors.
    """

    def __init__(
        self,
        parent,
        *,
        label: str,
        token: str,
        style: str,
        width: int,
        height: int,
    ) -> None:
        self.token = token
        self.base_color = FACE_BUTTON_COLORS.get(token, COLOR_BG_BTN)
        control_bg = (
            COLOR_SHELL_INSET if style in {"face", "stick", "dpad"}
            else COLOR_SHELL
        )
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=control_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        pad = 3
        if style == "face":
            self._shape = self.create_oval(
                pad, pad, width - pad, height - pad,
                fill=self.base_color,
                outline="#e8edf3",
                width=2,
            )
            font = ("Segoe UI Semibold", 13, "bold")
        else:
            radius = max(8, min(height // 2, 14))
            self._shape = self._create_round_rect(
                pad,
                pad,
                width - pad,
                height - pad,
                radius,
                fill=self.base_color,
                outline="#6e7d90",
                width=1,
            )
            font = ("Segoe UI Semibold", 7 if "\n" in label else 9, "bold")
        self._text = self.create_text(
            width / 2,
            height / 2,
            text=label,
            fill=COLOR_FG_BTN,
            font=font,
            justify="center",
        )

    def _create_round_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = (
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        )
        return self.create_polygon(points, smooth=True, splinesteps=20, **kwargs)

    def configure(self, cnf=None, **kw):
        color = kw.pop("bg", None)
        if color is not None and hasattr(self, "_shape"):
            if color == COLOR_BG_BTN:
                color = self.base_color
            self.itemconfigure(self._shape, fill=color)
        return super().configure(cnf, **kw)

    config = configure


class AnalogStickControl(tk.Canvas):
    """A real two-axis stick: drag the cap, release to spring back to centre."""

    SIZE = 132
    CENTER = SIZE / 2
    TRAVEL = 42.0
    CAP_RADIUS = 23.0
    DEADZONE = 0.08

    def __init__(self, parent) -> None:
        super().__init__(
            parent,
            width=self.SIZE,
            height=self.SIZE,
            bg=COLOR_SHELL_INSET,
            highlightthickness=0,
            bd=0,
            cursor="fleur",
            takefocus=0,
        )
        self.token = "LEFT_STICK"
        self._value = (0.0, 0.0)
        self._locked = False
        c = self.CENTER
        self.create_oval(
            5, 5, self.SIZE - 5, self.SIZE - 5,
            fill="#111820", outline="#64748a", width=2,
        )
        self.create_oval(
            c - self.TRAVEL, c - self.TRAVEL,
            c + self.TRAVEL, c + self.TRAVEL,
            fill="#202a36", outline="#3e4b5b", width=1,
        )
        self.create_line(c, 18, c, self.SIZE - 18, fill="#344253", width=1)
        self.create_line(18, c, self.SIZE - 18, c, fill="#344253", width=1)
        self._cap_shadow = self.create_oval(0, 0, 0, 0, fill="#080c11", outline="")
        self._cap = self.create_oval(0, 0, 0, 0, fill="#435267", outline="#e7edf5", width=2)
        self._cap_ring = self.create_oval(0, 0, 0, 0, fill="", outline="#718198", width=1)
        self._readout = self.create_text(
            c, self.SIZE - 16,
            text="X0.00  Y0.00",
            fill=COLOR_MUTED,
            font=("Cascadia Mono", 7, "bold"),
        )
        self.set_value(0.0, 0.0)

    @classmethod
    def value_from_point(cls, x: float, y: float) -> tuple[float, float]:
        dx = (float(x) - cls.CENTER) / cls.TRAVEL
        dy = -(float(y) - cls.CENTER) / cls.TRAVEL
        magnitude = (dx * dx + dy * dy) ** 0.5
        if magnitude > 1.0:
            dx /= magnitude
            dy /= magnitude
        if magnitude < cls.DEADZONE:
            return (0.0, 0.0)
        # Stable values keep recordings compact while preserving analog intent.
        return (round(dx, 2), round(dy, 2))

    def set_value(self, x: float, y: float, *, locked: Optional[bool] = None) -> None:
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        self._value = (x, y)
        if locked is not None:
            self._locked = locked
        c = self.CENTER
        cx = c + x * self.TRAVEL
        cy = c - y * self.TRAVEL
        r = self.CAP_RADIUS
        self.coords(self._cap_shadow, cx - r + 3, cy - r + 4, cx + r + 3, cy + r + 4)
        self.coords(self._cap, cx - r, cy - r, cx + r, cy + r)
        self.coords(self._cap_ring, cx - r + 6, cy - r + 6, cx + r - 6, cy + r - 6)
        color = COLOR_TOGGLE_HELD if self._locked else (
            COLOR_TAP_HELD if (x, y) != (0.0, 0.0) else "#435267"
        )
        self.itemconfigure(self._cap, fill=color)
        self.itemconfigure(
            self._readout,
            text=f"X{x:+.2f}  Y{y:+.2f}",
            fill="#dce6f2" if (x, y) != (0.0, 0.0) else COLOR_MUTED,
        )


class PadPanel(tk.Frame):
    """Renders one virtual Xbox controller with spatially accurate controls.

    Mouse semantics:
      * left-click  = press/hold: emits down on ButtonPress-1 and up on release
      * right-click = toggle: each click flips the held state (so the user can
        chord LB+A by right-clicking LB then left-tapping A)

    The panel is "dumb" — it just emits events into the event queue and
    paints its own visual state. The worker / tracker is the authoritative
    source of recorded data.
    """

    def __init__(self, parent, pad_idx: int, event_q: "queue.Queue", **kw):
        super().__init__(
            parent,
            bg=COLOR_BG_SURFACE,
            highlightbackground="#303a47",
            highlightthickness=1,
            **kw,
        )
        self.pad_idx = pad_idx
        self.q = event_q
        self._widgets: dict[str, tk.Button] = {}
        self._kind_of: dict[str, str] = {}
        self._toggled: dict[str, bool] = {}  # token → True if currently right-click-held
        self._tap_active: dict[str, bool] = {}  # token → True between BPress and BRelease
        self._last_left_t: dict[str, float] = {}   # token → monotonic time of last accepted left-press
        self._last_right_t: dict[str, float] = {}  # token → monotonic time of last accepted right-press
        self._stick_drag_active = False
        self._last_stick_value = (0.0, 0.0)
        header = tk.Frame(self, bg=COLOR_BG_SURFACE)
        header.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(
            header,
            text=f"PAD {pad_idx}",
            bg=COLOR_BG_SURFACE,
            fg=COLOR_FG_BTN,
            font=("Segoe UI Semibold", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text=f"XINPUT SLOT {pad_idx}  /  XBOX LAYOUT",
            bg=COLOR_BG_SURFACE,
            fg=COLOR_MUTED,
            font=("Cascadia Mono", 8),
        ).pack(side="right")
        self.canvas = tk.Canvas(
            self,
            bg=COLOR_BG_SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", self._draw_controller)
        self._build()

    def _debounced(self, token: str, last_map: dict[str, float]) -> bool:
        """True if this press is too close to the previous one on the same
        button (an accidental double-click). Records the time when accepted."""
        if DEBOUNCE_S <= 0:
            return False
        now = time.monotonic()
        if now - last_map.get(token, 0.0) < DEBOUNCE_S:
            return True
        last_map[token] = now
        return False

    def _build(self) -> None:
        triggers = {"LT": "trigger_l", "RT": "trigger_r"}
        self.left_stick = AnalogStickControl(self.canvas)
        self.left_stick.place(
            relx=LEFT_STICK_LAYOUT[0], rely=LEFT_STICK_LAYOUT[1], anchor="center",
        )
        for token, (relx, rely, label, style) in PAD_CONTROL_LAYOUT.items():
            kind = triggers.get(token, "button")
            width, height = PAD_CONTROL_SIZES[style]
            btn = PadControl(
                self.canvas,
                label=label,
                token=token,
                style=style,
                width=width,
                height=height,
            )
            btn.place(relx=relx, rely=rely, anchor="center")
            self._widgets[token] = btn
            self._kind_of[token] = kind

    def _emit_stick_axis(self, value: tuple[float, float]) -> None:
        if value == self._last_stick_value:
            return
        self._last_stick_value = value
        self.left_stick.set_value(*value)
        self.q.put({
            'type': 'pad', 't': time.monotonic(),
            'pad': self.pad_idx, 'kind': 'stick_l_axis',
            'token': value, 'action': 'set',
        })

    def _on_stick_press(self, x: float, y: float) -> None:
        self._stick_drag_active = True
        self.left_stick._locked = False
        self._emit_stick_axis(self.left_stick.value_from_point(x, y))

    def _on_stick_drag(self, x: float, y: float) -> None:
        if self._stick_drag_active:
            self._emit_stick_axis(self.left_stick.value_from_point(x, y))

    def _on_stick_release(self) -> None:
        if not self._stick_drag_active:
            return
        self._stick_drag_active = False
        self.left_stick._locked = False
        self._emit_stick_axis((0.0, 0.0))

    def _on_stick_toggle(self, x: float, y: float) -> None:
        """Right-click locks an analog vector; right-click again recentres."""
        self._stick_drag_active = False
        if self.left_stick._locked:
            self.left_stick._locked = False
            self._emit_stick_axis((0.0, 0.0))
            return
        value = self.left_stick.value_from_point(x, y)
        if value == (0.0, 0.0):
            return
        self.left_stick._locked = True
        self._emit_stick_axis(value)
        self.left_stick.set_value(*value, locked=True)

    def _draw_controller(self, _event=None) -> None:
        """Draw a responsive Xbox-style shell behind the clickable controls."""
        c = self.canvas
        c.delete("controller_art")
        w = max(c.winfo_width(), 520)
        h = max(c.winfo_height(), 410)

        def xy(points):
            return [value for x, y in points for value in (x * w, y * h)]

        shell = [
            (0.17, 0.17), (0.31, 0.09), (0.44, 0.14),
            (0.56, 0.14), (0.69, 0.09), (0.83, 0.17),
            (0.95, 0.62), (0.89, 0.90), (0.73, 0.74),
            (0.62, 0.69), (0.38, 0.69), (0.27, 0.74),
            (0.11, 0.90), (0.05, 0.62),
        ]
        c.create_polygon(
            xy([(x + 0.006, y + 0.018) for x, y in shell]),
            smooth=True,
            splinesteps=36,
            fill="#080b0f",
            outline="",
            tags="controller_art",
        )
        c.create_polygon(
            xy(shell),
            smooth=True,
            splinesteps=36,
            fill=COLOR_SHELL,
            outline=COLOR_SHELL_EDGE,
            width=2,
            tags="controller_art",
        )

        # Hardware zones make the mapping legible before the labels are read.
        c.create_oval(
            0.09 * w, 0.24 * h, 0.41 * w, 0.58 * h,
            fill=COLOR_SHELL_INSET, outline="#465366", width=2,
            tags="controller_art",
        )
        c.create_oval(
            0.61 * w, 0.24 * h, 0.91 * w, 0.58 * h,
            fill=COLOR_SHELL_INSET, outline="#465366", width=2,
            tags="controller_art",
        )
        c.create_oval(
            0.27 * w, 0.56 * h, 0.51 * w, 0.84 * h,
            fill=COLOR_SHELL_INSET, outline="#465366", width=2,
            tags="controller_art",
        )
        # Right-stick placeholder completes the controller silhouette while
        # clearly communicating that it is not part of this recorder yet.
        c.create_oval(
            0.56 * w, 0.58 * h, 0.70 * w, 0.76 * h,
            fill="#202832", outline="#4a5666", width=2,
            tags="controller_art",
        )
        c.create_oval(
            0.585 * w, 0.61 * h, 0.675 * w, 0.73 * h,
            fill="#141a21", outline="#35404d", width=1,
            tags="controller_art",
        )

        c.create_rectangle(
            0.115 * w, 0.255 * h, 0.385 * w, 0.305 * h,
            fill=COLOR_SHELL_INSET, outline="",
            tags="controller_art",
        )
        c.create_rectangle(
            0.655 * w, 0.255 * h, 0.865 * w, 0.305 * h,
            fill=COLOR_SHELL_INSET, outline="",
            tags="controller_art",
        )
        c.create_text(
            0.25 * w, 0.28 * h,
            text="LEFT STICK  /  MOVEMENT",
            fill=COLOR_MUTED,
            font=("Cascadia Mono", 8, "bold"),
            tags="controller_art",
        )
        c.create_text(
            0.76 * w, 0.28 * h,
            text="FACE BUTTONS",
            fill=COLOR_MUTED,
            font=("Cascadia Mono", 8, "bold"),
            tags="controller_art",
        )
        c.create_text(
            0.39 * w, 0.88 * h,
            text="MENU NAVIGATION  /  D-PAD",
            fill=COLOR_MUTED,
            font=("Cascadia Mono", 8, "bold"),
            tags="controller_art",
        )
        c.create_text(
            0.63 * w, 0.82 * h,
            text="R STICK\nNOT MAPPED",
            fill="#718093",
            font=("Cascadia Mono", 7, "bold"),
            justify="center",
            tags="controller_art",
        )

    # ---- mouse handlers ----

    def _on_left_press(self, token: str, kind: str) -> None:
        if DEBUG_UI:
            print(f"[ui]  pad{self.pad_idx} {token} left-press   (kind={kind})")
        if self._toggled.get(token):
            if DEBUG_UI:
                print(f"[ui]  pad{self.pad_idx} {token} ignored: already toggle-held")
            return
        # Drop accidental double-clicks. An ignored press never sets _tap_active,
        # so its matching ButtonRelease is auto-ignored by _on_left_release.
        if self._debounced(token, self._last_left_t):
            if DEBUG_UI:
                print(f"[ui]  pad{self.pad_idx} {token} ignored: debounce "
                      f"(<{int(DEBOUNCE_S * 1000)}ms since last tap)")
            return
        self._tap_active[token] = True
        self._widgets[token].configure(bg=COLOR_TAP_HELD)
        self.q.put({
            'type': 'pad', 't': time.monotonic(),
            'pad': self.pad_idx, 'kind': kind, 'token': token, 'action': 'down',
        })

    def _on_left_release(self, token: str, kind: str) -> None:
        if DEBUG_UI:
            print(f"[ui]  pad{self.pad_idx} {token} left-release (kind={kind})")
        if not self._tap_active.get(token):
            return
        self._tap_active[token] = False
        # If toggle-held, keep it green; else back to default.
        if not self._toggled.get(token):
            self._widgets[token].configure(bg=COLOR_BG_BTN)
        else:
            self._widgets[token].configure(bg=COLOR_TOGGLE_HELD)
        self.q.put({
            'type': 'pad', 't': time.monotonic(),
            'pad': self.pad_idx, 'kind': kind, 'token': token, 'action': 'up',
        })

    def _on_right_press(self, token: str, kind: str) -> None:
        if DEBUG_UI:
            print(f"[ui]  pad{self.pad_idx} {token} right-click toggle")
        # Drop accidental double right-clicks (they'd toggle on then off again).
        if self._debounced(token, self._last_right_t):
            if DEBUG_UI:
                print(f"[ui]  pad{self.pad_idx} {token} ignored: debounce "
                      f"(<{int(DEBOUNCE_S * 1000)}ms since last toggle)")
            return
        currently = self._toggled.get(token, False)
        self._toggled[token] = not currently
        if self._toggled[token]:
            self._widgets[token].configure(bg=COLOR_TOGGLE_HELD)
            self.q.put({
                'type': 'pad', 't': time.monotonic(),
                'pad': self.pad_idx, 'kind': kind, 'token': token, 'action': 'down',
            })
        else:
            self._widgets[token].configure(bg=COLOR_BG_BTN)
            self.q.put({
                'type': 'pad', 't': time.monotonic(),
                'pad': self.pad_idx, 'kind': kind, 'token': token, 'action': 'up',
            })

    def release_all_held(self) -> None:
        """Drop every toggle-held + tap-active button on this pad.
        Called by the global RELEASE-ALL control."""
        t = time.monotonic()
        if self._last_stick_value != (0.0, 0.0):
            self._stick_drag_active = False
            self.left_stick._locked = False
            self._last_stick_value = (0.0, 0.0)
            self.left_stick.set_value(0.0, 0.0)
            self.q.put({
                'type': 'pad', 't': t,
                'pad': self.pad_idx, 'kind': 'stick_l_axis',
                'token': (0.0, 0.0), 'action': 'set',
            })
        for token, held in list(self._toggled.items()):
            if held:
                self._toggled[token] = False
                self._widgets[token].configure(bg=COLOR_BG_BTN)
                self.q.put({
                    'type': 'pad', 't': t,
                    'pad': self.pad_idx, 'kind': self._kind_of[token], 'token': token, 'action': 'up',
                })
        for token, active in list(self._tap_active.items()):
            if active:
                self._tap_active[token] = False
                self._widgets[token].configure(bg=COLOR_BG_BTN)
                self.q.put({
                    'type': 'pad', 't': t,
                    'pad': self.pad_idx, 'kind': self._kind_of[token], 'token': token, 'action': 'up',
                })

    def reflect_state(
        self,
        held_buttons: set[str],
        left_trigger: float,
        right_trigger: float,
        left_stick: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """Authoritative redraw based on the tracker's view of this pad's state.
        Visual buttons not in the held set are reset to default unless they're
        currently mid-tap or toggle-held (those keep their richer colors).
        """
        # Triggers visualized as buttons too.
        all_held = set(held_buttons)
        if left_trigger > 0:
            all_held.add('LT')
        if right_trigger > 0:
            all_held.add('RT')
        self.left_stick.set_value(
            *left_stick,
            locked=self.left_stick._locked and left_stick != (0.0, 0.0),
        )
        for token, btn in self._widgets.items():
            if self._toggled.get(token):
                continue  # leave green
            if self._tap_active.get(token):
                continue  # leave yellow until release
            if token in all_held:
                btn.configure(bg=COLOR_TOGGLE_HELD)
            else:
                btn.configure(bg=COLOR_BG_BTN)


class CaptureWindow:
    """Tk root that hosts two PadPanels + control buttons. Pure UI — pushes
    dict events into the queue, never touches vgamepad/IO directly.
    """

    def __init__(self, event_q: "queue.Queue", n_pads: int = 2):
        self.q = event_q
        self.n_pads = n_pads
        self._status_q: "queue.Queue" = queue.Queue()
        self._guard_mouse_q: "queue.Queue[tuple[int, int, int]]" = queue.Queue()
        self._guard_pressed: dict[int, object] = {}
        # Pointer/window origins while the dedicated move handle is held.
        # Native title-bar dragging cannot work under physical-input isolation:
        # the low-level hook consumes button down/up before Windows sees them.
        self._window_drag_origin: Optional[tuple[int, int, int, int]] = None
        self._escape_banner = None
        # Set by main() to PhysicalInputIsolation.heartbeat once the hooks are
        # live; the drain tick calls it so the watchdog can see this thread.
        self.heartbeat = lambda: None
        self._terminal_control_sent = False
        self._last_control_t: dict[str, float] = {}
        # Where the cursor was when the operator last pressed a pad control.
        # Games with controller support enabled drive the SYSTEM cursor from the
        # pad — Slay the Spire warps it onto whichever UI element the stick/dpad
        # just focused — which yanks the pointer off this window mid-recording.
        # We put it back; see _pin_cursor_against_warp.
        self._cursor_anchor: Optional[tuple[int, int]] = None
        self._cursor_anchor_until = 0.0
        # Make this process DPI-aware on Windows BEFORE creating Tk, so the
        # window geometry is reported in physical pixels (otherwise on a
        # 125%-scaled display the window is rendered 1.25× larger than
        # Tk thinks, causing layout overflow + screenshot misalignment).
        if sys.platform == "win32":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        self.root = tk.Tk()
        self.root.title("Pad Recorder · Xbox Calibration Deck")
        window_width = 980 if n_pads == 1 else 1500
        min_width = 900 if n_pads == 1 else 1220
        self.root.geometry(f"{window_width}x700+40+40")
        self.root.minsize(min_width, 620)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLOR_BG_PANEL)
        # CRITICAL for game overlays: WS_EX_NOACTIVATE makes left-clicks on the
        # recorder window NOT activate it as the foreground window. Without
        # this, clicking any button steals focus from games that require it
        # to read XInput, so our clicks
        # arrive at vgamepad but the game's input loop ignores them.
        # Same trick used by Steam Overlay / OBS click-overlay.
        if sys.platform == "win32":
            self._apply_noactivate_style()

        # ---- top toolbar (control buttons + status line) ----
        toolbar = tk.Frame(self.root, bg=COLOR_BG_PANEL)
        toolbar.pack(fill="x", padx=12, pady=(12, 5))
        self._mk_ctrl(toolbar, "💾 SAVE", "stop_save", "#2ea043").pack(side="left", padx=2)
        self._mk_ctrl(toolbar, "✖ CANCEL", "cancel", "#da3633").pack(side="left", padx=2)
        self._mk_ctrl(toolbar, "★ CHECKPOINT", "checkpoint", "#1f6feb").pack(side="left", padx=2)
        self._mk_ctrl(toolbar, "⤬ RELEASE ALL",   "release_all","#8957e5").pack(side="left", padx=2)

        self._window_drag_handle = tk.Label(
            toolbar,
            text="↔ MOVE WINDOW",
            bg="#30363d",
            fg="#f0f6fc",
            activebackground="#484f58",
            activeforeground="#ffffff",
            font=("Segoe UI Semibold", 9, "bold"),
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="fleur",
        )
        self._window_drag_handle.pack(side="right", padx=(10, 2))

        # ---- pads side-by-side (P2 panel only for 2-player games: a lone
        # phantom second XInput pad confuses 1P games' controller handling,
        # and mouse-driven input means no panel = no P2 events at all) ----
        pads_frame = tk.Frame(self.root, bg=COLOR_BG_PANEL)
        pads_frame.pack(fill="both", expand=True, padx=12, pady=5)
        self.pad1 = PadPanel(pads_frame, 1, event_q)
        self.pad1.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.pad2 = None
        if self.n_pads >= 2:
            self.pad2 = PadPanel(pads_frame, 2, event_q)
            self.pad2.pack(side="left", fill="both", expand=True, padx=(4, 0))

        tk.Label(
            self.root,
            text=("BUTTONS: L = TAP  /  R = HOLD      "
                  "LEFT STICK: DRAG = ANALOG  /  R = LOCK"),
            font=("Segoe UI", 9),
            fg=COLOR_MUTED,
            bg=COLOR_BG_PANEL,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(3, 0))

        # ---- live status bar ----
        self.label = tk.Label(
            self.root,
            text="READY  ·  Drag LEFT STICK for analog movement; D-PAD is digital input",
            font=("Cascadia Mono", 9, "bold"),
            fg="#62d6c7",
            bg=COLOR_BG_SURFACE,
            anchor="w",
            padx=10,
            pady=7,
        )
        self.label.pack(fill="x", padx=12, pady=(4, 12))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.stop_reason: str = ""
        self.root.after(50, self._drain_status)
        self.root.after(8, self._drain_guard_mouse)

    # ---- builders ----

    def _mk_ctrl(self, parent, label: str, token: str, color: str) -> tk.Button:
        btn = tk.Button(
            parent, text=label,
            bg=color, fg="white",
            activebackground=color,
            activeforeground="white",
            font=("Segoe UI Semibold", 9, "bold"),
            relief="flat", bd=0, takefocus=0,
            padx=11, pady=7,
            cursor="hand2",
            command=lambda t=token: self._enqueue_ctrl(t),
        )
        return btn

    def _apply_noactivate_style(self) -> None:
        """Set WS_EX_NOACTIVATE on the recorder's TOP-LEVEL hwnd so clicks
        don't steal foreground from the game.

        Critical implementation detail: the previous version called
        `GetParent(self.root.winfo_id())` to find the hwnd, but on modern
        Tk that returns the wrong handle — the visible top-level window
        ends up WITHOUT the NOACTIVATE bit, clicks steal foreground, and
        the game (which polls XInput only while fg) drops every press.

        Use `wm_frame()` — Tk's documented API that returns the OS-level
        window-manager frame hwnd as a hex string. THIS is the visible
        window the user clicks on; this is the one that must not activate.
        After setting the style, call SetWindowPos(SWP_FRAMECHANGED) so
        Windows flushes the cached style, then verify by reading it back.
        If the readback shows the bit didn't stick, scream loudly — the
        recorder will be unusable.
        """
        try:
            # CRITICAL: wm_frame() returns a stale inner-content hwnd until
            # the window has been mapped. Pump the event loop once so Tk
            # creates the OS-level window-manager frame, otherwise we'd be
            # decorating a hidden hwnd and the *visible* top-level (the one
            # users click on) would still steal foreground.
            self.root.update_idletasks()
            self.root.update()
            frame_hex = self.root.wm_frame()  # e.g. '0x123abc'
            hwnd = int(frame_hex, 16)
        except Exception as e:
            print(f"[recorder] WARN: could not resolve wm_frame() hwnd: {e}",
                  file=sys.stderr)
            return
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        try:
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = style | WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            # Flush the cached frame so the new ex-style takes effect now,
            # not on the next implicit redraw.
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            # Read back — if the bit didn't stick, the recorder is broken.
            verify = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if verify & WS_EX_NOACTIVATE:
                print(f"[recorder] WS_EX_NOACTIVATE applied to hwnd={hwnd} "
                      f"(style 0x{style:08X} → 0x{verify:08X})")
            else:
                print(
                    f"[recorder] FATAL: WS_EX_NOACTIVATE did not stick on "
                    f"hwnd={hwnd} (style still 0x{verify:08X}). "
                    f"Clicks WILL steal foreground from the game — input "
                    f"will be dropped. File a bug with the Tk version "
                    f"({self.root.tk.call('info', 'patchlevel')}).",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[recorder] WARN: WS_EX_NOACTIVATE failed: {e}",
                  file=sys.stderr)

    def _enqueue_ctrl(self, token: str) -> None:
        now = time.monotonic()
        if token in ("stop_save", "cancel"):
            if self._terminal_control_sent:
                return
            self._terminal_control_sent = True
        elif token == "checkpoint":
            # Held shortcuts can repeat before the next recorded action.
            if now - self._last_control_t.get(token, 0.0) < 0.2:
                return
        self._last_control_t[token] = now
        if token == "release_all":
            # Release-all is a UI concern: drop both panels' visuals AND
            # emit the appropriate pad up events. Then emit the control
            # event so the worker logs it.
            self.pad1.release_all_held()
            if self.pad2 is not None:
                self.pad2.release_all_held()
        self.q.put({'type': 'control', 't': now, 'token': token})

    # ---- status drain ----

    def _drain_status(self) -> None:
        latest = None
        try:
            while True:
                latest = self._status_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            text, color, pad1_state, pad2_state = latest
            try:
                self.label.configure(text=text, fg=color)
                self.pad1.reflect_state(*pad1_state)
                if self.pad2 is not None:
                    self.pad2.reflect_state(*pad2_state)
            except Exception:
                pass
        try:
            self.root.after(50, self._drain_status)
        except RuntimeError:
            pass

    # ---- API used by RecorderWorker (thread-safe) ----

    def set_status(
        self, text: str, color: str,
        pad1_state: tuple, pad2_state: tuple,
    ) -> None:
        """Thread-safe: enqueue the full status snapshot for the next drain tick."""
        self._status_q.put((text, color, pad1_state, pad2_state))

    def _on_close(self) -> None:
        self._enqueue_ctrl('cancel')

    def run(self) -> None:
        self.root.mainloop()

    def show_escape_banner(self) -> None:
        """Pin a always-visible strip listing the ONLY keys that still work.

        With the isolation hooks installed every other key and click is
        swallowed, so an operator who does not know these three chords cannot
        tell a working recorder from a frozen machine — the reported symptom
        was a hard reboot. Ctrl+Alt+Del is listed because it is a kernel-level
        SAS that no hook can intercept: it always gets to Task Manager.
        """
        if self._escape_banner is not None:
            return
        banner = tk.Toplevel(self.root)
        banner.withdraw()
        banner.overrideredirect(True)
        banner.attributes("-topmost", True)
        banner.configure(bg="#7d2b0b")
        tk.Label(
            banner,
            text=ESCAPE_BANNER_TEXT,
            font=("Segoe UI Semibold", 10, "bold"),
            fg="#ffe9c9", bg="#7d2b0b", padx=14, pady=6,
        ).pack()
        banner.update_idletasks()
        width = banner.winfo_reqwidth()
        x = max(0, (banner.winfo_screenwidth() - width) // 2)
        banner.geometry(f"+{x}+8")
        banner.deiconify()
        _make_click_through(banner)
        banner.lift()
        self._escape_banner = banner

    def lift_escape_banner(self) -> None:
        """Re-assert the banner above the game, which re-tops itself on focus."""
        if self._escape_banner is None:
            return
        try:
            self._escape_banner.lift()
        except tk.TclError:
            self._escape_banner = None

    # -- explicit window movement -----------------------------------------

    def _is_window_drag_widget(self, widget) -> bool:
        """Whether *widget* is the dedicated move handle or one of its children."""
        current = widget
        while current is not None:
            if current is self._window_drag_handle:
                return True
            current = getattr(current, "master", None)
        return False

    def _start_window_drag(self, pointer_x: int, pointer_y: int) -> None:
        """Remember pointer and window origins for one absolute-position drag."""
        self._release_cursor_anchor()
        self._window_drag_origin = (
            int(pointer_x),
            int(pointer_y),
            int(self.root.winfo_x()),
            int(self.root.winfo_y()),
        )

    def _move_window_drag(self, pointer_x: int, pointer_y: int) -> None:
        origin = self._window_drag_origin
        if origin is None:
            return
        pointer_start_x, pointer_start_y, window_start_x, window_start_y = origin
        window_x = window_start_x + int(pointer_x) - pointer_start_x
        window_y = window_start_y + int(pointer_y) - pointer_start_y
        # Signed offsets are valid on multi-monitor desktops where a display
        # can sit to the left/above the primary monitor.
        self.root.geometry(f"{window_x:+d}{window_y:+d}")

    def _finish_window_drag(self, pointer_x: int, pointer_y: int) -> None:
        if self._window_drag_origin is None:
            return
        self._move_window_drag(pointer_x, pointer_y)
        self._window_drag_origin = None

    def queue_guarded_mouse(self, message: int, x: int, y: int) -> None:
        """Thread-safe entry point used by the low-level mouse hook."""
        self._guard_mouse_q.put((message, x, y))

    @staticmethod
    def _pad_control_owner(widget):
        current = widget
        control = None
        panel = None
        while current is not None:
            if control is None and isinstance(current, (PadControl, AnalogStickControl)):
                control = current
            if isinstance(current, PadPanel):
                panel = current
                break
            current = getattr(current, "master", None)
        return panel, control

    def _guard_widget_at(self, x: int, y: int):
        """Return the recorder widget under the given screen point."""
        return self.root.winfo_containing(x, y)

    def _dispatch_guard_mouse(self, message: int, x: int, y: int) -> None:
        if message == WM_MOUSEMOVE:
            if self._window_drag_origin is not None:
                self._move_window_drag(x, y)
                return
            widget = self._guard_pressed.get(1)
            panel, control = self._pad_control_owner(widget)
            if panel is not None and isinstance(control, AnalogStickControl):
                # A stick drag IS deliberate cursor movement, so stop defending
                # the press anchor or the drag would fight the restore.
                self._release_cursor_anchor()
                panel._on_stick_drag(
                    x - control.winfo_rootx(),
                    y - control.winfo_rooty(),
                )
            return

        button = 1 if message in (WM_LBUTTONDOWN, WM_LBUTTONUP) else 3
        is_down = message in (WM_LBUTTONDOWN, WM_RBUTTONDOWN)
        if is_down:
            widget = self._guard_widget_at(x, y)
            self._guard_pressed[button] = widget
        else:
            widget = self._guard_pressed.pop(button, None)
            if widget is None:
                widget = self._guard_widget_at(x, y)

        if (message == WM_LBUTTONDOWN
                and self._is_window_drag_widget(widget)):
            self._start_window_drag(x, y)
            return
        if message == WM_LBUTTONUP and self._window_drag_origin is not None:
            self._finish_window_drag(x, y)
            return

        panel, control = self._pad_control_owner(widget)
        if panel is not None and isinstance(control, AnalogStickControl):
            local_x = x - control.winfo_rootx()
            local_y = y - control.winfo_rooty()
            if message == WM_LBUTTONDOWN:
                # No anchor: pressing the stick starts a drag, where the
                # operator moves the cursor on purpose.
                self._release_cursor_anchor()
                panel._on_stick_press(local_x, local_y)
            elif message == WM_LBUTTONUP:
                panel._on_stick_release()
                # Releasing centres the stick, which itself moves the game's
                # focus and can warp the cursor, so defend from here.
                self._anchor_cursor_here()
            elif message == WM_RBUTTONDOWN:
                panel._on_stick_toggle(local_x, local_y)
                self._anchor_cursor_here()
            return
        if panel is not None and control is not None:
            token = control.token
            kind = panel._kind_of[token]
            # Anchor BEFORE driving the pad: the warp is the game's reaction to
            # the input, so the position to defend is the one from before it.
            self._anchor_cursor_here()
            if message == WM_LBUTTONDOWN:
                panel._on_left_press(token, kind)
            elif message == WM_LBUTTONUP:
                panel._on_left_release(token, kind)
            elif message == WM_RBUTTONDOWN:
                panel._on_right_press(token, kind)
            return

        # Toolbar buttons use normal Tk command callbacks. Invoke on release so
        # a press-drag-away does not accidentally save/cancel the recording.
        if message == WM_LBUTTONUP and isinstance(widget, tk.Button):
            try:
                widget.invoke()
            except tk.TclError:
                pass

    def _drain_guard_mouse(self) -> None:
        # Liveness proof for the isolation watchdog. This tick IS the click
        # delivery path, so if it stops the recorder is unusable anyway and the
        # hooks must come off.
        self.heartbeat()
        try:
            while True:
                message, x, y = self._guard_mouse_q.get_nowait()
                self._dispatch_guard_mouse(message, x, y)
        except queue.Empty:
            pass
        self._pin_cursor_against_warp()
        try:
            self.root.after(8, self._drain_guard_mouse)
        except (RuntimeError, tk.TclError):
            pass

    # -- cursor pinning ----------------------------------------------------
    #
    # A game in controller mode drives the system cursor from pad input. Slay
    # the Spire warps it onto whichever UI element the pad just focused, so a
    # single click on this window's pad buttons teleports the pointer into the
    # game and the operator has to re-aim before every step. This did not show
    # up until the pads were created before launch, because until then StS
    # never enumerated a controller and so never entered controller mode.
    #
    # ClipCursor(NULL) does not help: it lifts a containment boundary, and this
    # is a SetCursorPos. So we remember where the operator clicked and put the
    # cursor back for a short window afterwards.
    #
    # The recording itself is unaffected. Only pad state and wall-clock timing
    # are recorded; the physical cursor is the operator's aiming device, not
    # input to the artifact. Restores are injected, and the low-level hook drops
    # injected events (LLMHF_INJECTED), so a restore cannot be mistaken for an
    # operator gesture.

    # How long after a pad press to keep pulling the cursor back. The warp is a
    # reaction to the input, so it lands a frame or two later — one restore on
    # the click itself would race it and usually lose.
    CURSOR_PIN_SECONDS = 0.45
    # Ignore sub-pixel drift so the operator's own small movements still work.
    CURSOR_PIN_SLACK_PX = 3

    def _anchor_cursor_here(self) -> None:
        """Remember the cursor position to defend, called on a pad press."""
        pos = _get_cursor_pos()
        if pos is None:
            return
        self._cursor_anchor = pos
        self._cursor_anchor_until = time.monotonic() + self.CURSOR_PIN_SECONDS

    def _release_cursor_anchor(self) -> None:
        """Stop defending — the operator is dragging or has let go."""
        self._cursor_anchor = None
        self._cursor_anchor_until = 0.0

    def _pin_cursor_against_warp(self) -> None:
        """Put the cursor back if the game moved it away from the last press."""
        anchor = self._cursor_anchor
        if anchor is None:
            return
        if time.monotonic() >= self._cursor_anchor_until:
            self._release_cursor_anchor()
            return
        pos = _get_cursor_pos()
        if pos is None:
            self._release_cursor_anchor()
            return
        if (abs(pos[0] - anchor[0]) <= self.CURSOR_PIN_SLACK_PX
                and abs(pos[1] - anchor[1]) <= self.CURSOR_PIN_SLACK_PX):
            return
        _set_cursor_pos(*anchor)

    def stop(self, reason: str) -> None:
        self.stop_reason = reason
        try:
            self.root.quit()
        except Exception:
            pass


# --------------------- worker thread ---------------------

class RecorderWorker(threading.Thread):
    """Owns the pad state tracker and XInput driver. Drains dict events from the queue.

    Event protocol (see CaptureWindow):
      {'type': 'pad',     ...}  → tracker.on_event + driver.apply_state
      {'type': 'control', ...}  → stop_save / cancel / checkpoint / release_all
      {'type': 'system',  ...}  → shutdown signal
    """

    def __init__(
        self,
        event_q: "queue.Queue",
        driver: XInputDriver,
        win: CaptureWindow,
    ):
        super().__init__(daemon=True)
        self.q = event_q
        self.driver = driver
        self.win = win
        self.tracker = PadStateTracker()
        self.stop_reason: str = ""

    def run(self) -> None:
        while True:
            try:
                ev = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if not isinstance(ev, dict):
                continue  # legacy/unknown event — drop
            etype = ev.get('type')
            if etype == 'system' and ev.get('token') == 'shutdown':
                return
            if etype == 'control':
                token = ev.get('token')
                t = ev.get('t', time.monotonic())
                if token == 'stop_save':
                    self.stop_reason = 'save'
                    self.tracker.finalize(t)
                    self.win.stop('save')
                    return
                if token == 'cancel':
                    self.stop_reason = 'cancel'
                    self.win.stop('cancel')
                    return
                if token == 'checkpoint':
                    self.tracker.mark_checkpoint()
                    self._refresh_status(extra="CHECKPOINT marked", color="yellow")
                if token == 'release_all':
                    # The panels already emitted their 'up' events visually.
                    # Just refresh the status line so the user sees the count.
                    self._refresh_status(extra="released all held", color="orange")
                continue
            if etype == 'pad':
                if DEBUG_WORKER:
                    print(f"[wkr] dequeued pad event: pad={ev.get('pad')} "
                          f"{ev.get('action')} kind={ev.get('kind')} token={ev.get('token')!r}")
                try:
                    self.tracker.on_event(
                        ev['t'], ev['pad'], ev['kind'], ev['token'], ev['action'],
                    )
                except (KeyError, ValueError) as e:
                    if DEBUG_WORKER:
                        print(f"[wkr] tracker rejected event: {e}")
                    continue
                pad = ev['pad']
                ps = self.tracker.pads[pad]
                buttons = [resolve_button(button) for button in sorted(ps.buttons)]
                self.driver.apply_state(
                    pad - 1,
                    buttons=buttons,
                    left_stick=ps.left_stick,
                    right_stick=(0.0, 0.0),
                    left_trigger=ps.left_trigger,
                    right_trigger=ps.right_trigger,
                )
                self._refresh_status()

    def _refresh_status(self, extra: str = "", color: Optional[str] = None) -> None:
        n = len(self.tracker.steps)
        msg = f"recording — {n} step(s)"
        if extra:
            msg += f" — {extra}"
        if self.tracker.overlap_detected:
            msg += " — ⚠ overlap"
            color = color or "yellow"
        if color is None:
            color = "lime"
        p1 = self.tracker.pads[1]
        p2 = self.tracker.pads[2]
        self.win.set_status(
            msg, color,
            (set(p1.buttons), p1.left_trigger, p1.right_trigger, p1.left_stick),
            (set(p2.buttons), p2.left_trigger, p2.right_trigger, p2.left_stick),
        )


# --------------------- launch helper ---------------------

def _close_recorder_pads(driver: XInputDriver) -> None:
    try:
        driver.reset()
    finally:
        driver.close()


def _force_game_foreground(process_name: str) -> bool:
    """Use the shared nonblocking foreground request without synthetic keys."""
    if sys.platform != "win32":
        return False
    from coding4game_gui.benchmark.windows.auto_nav import find_game_hwnd, force_foreground
    hit = find_game_hwnd(process_name)
    return bool(hit and force_foreground(hit[0]))


def _release_cursor_clip() -> bool:
    """Let the mouse reach the recorder while a foreground game is running.

    Some games (notably UE titles) confine the cursor to their client area
    even in windowed mode.  The recorder deliberately keeps the game in the
    foreground so it continues polling XInput, which means ordinary focus
    switching cannot release that confinement.  Passing NULL to ClipCursor
    releases it without changing foreground ownership.

    This does NOT defend against a game that MOVES the cursor: releasing the
    clip lifts a boundary, it does not stop a SetCursorPos.  See
    CaptureWindow._pin_cursor_against_warp for that.
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.ClipCursor(None))
    except Exception:
        return False


def _get_cursor_pos() -> Optional[tuple[int, int]]:
    """Current cursor position in screen coordinates, or None off-Windows."""
    if sys.platform != "win32":
        return None
    try:
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)
    except Exception:
        return None


def _set_cursor_pos(x: int, y: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
    except Exception:
        return False


class _MouseHookPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MouseHookData(ctypes.Structure):
    _fields_ = [
        ("pt", _MouseHookPoint),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KeyboardHookData(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# The isolation hooks swallow EVERY physical key and mouse event, so the
# desktop is only usable for as long as the recorder's Tk thread is alive to
# drain them and honour Ctrl+Alt+Q. If that thread wedges — the observed case
# is _force_game_foreground blocking inside AttachThreadInput /
# SetForegroundWindow against a hung game window — the hooks stay installed
# with nobody left to release them, and the operator's keyboard and mouse are
# dead until a hard reboot. The watchdog below makes that state self-healing.
WATCHDOG_TIMEOUT_S: float = 3.0
WATCHDOG_POLL_S: float = 0.2


def _watchdog_should_trip(
    last_beat: float, now: float, *, armed: bool, timeout_s: float,
) -> bool:
    """True when the Tk thread has gone silent long enough to be presumed dead.

    Pure seam so the trip condition is unit-testable without threads or hooks.
    `armed` stays False until the first heartbeat arrives, so a slow startup
    (game launch, vgamepad probing) is never mistaken for a deadlock.
    """
    if not armed or timeout_s <= 0:
        return False
    return (now - last_beat) >= timeout_s


class PhysicalInputIsolation(threading.Thread):
    """Consume physical keyboard/mouse input while the game owns foreground.

    ViGEm creates a real virtual Xbox controller, but it does not hide the
    operator's physical mouse.  Keeping a game foreground without this guard
    creates two simultaneous actions: XInput from the pad UI plus mouse/keyboard
    Raw Input in the game.  The hooks below consume the hardware events and
    forward only virtual-cursor actions to the recorder.
    """

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012
    LLKHF_INJECTED = 0x00000010
    CONTROL_VKS = {0x11, 0xA2, 0xA3}  # generic, left, right Ctrl
    ALT_VKS = {0x12, 0xA4, 0xA5}      # generic, left, right Alt
    CONTROL_TOKENS = {
        ord("S"): "stop_save",
        ord("Q"): "cancel",
        ord("C"): "checkpoint",
    }

    def __init__(self, mouse_sink, control_sink):
        super().__init__(daemon=True)
        self.mouse_sink = mouse_sink
        self.control_sink = control_sink
        self.ready = threading.Event()
        self.thread_id: Optional[int] = None
        self.active = False
        self.error = ""
        self._mouse_hook = None
        self._keyboard_hook = None
        self._mouse_proc = None
        self._keyboard_proc = None
        self._ctrl_down = False
        self._alt_down = False
        # Watchdog state. `_last_beat` is written by the Tk main thread via
        # heartbeat() and read by the watchdog thread; a bare float write is
        # atomic under the GIL, so it needs no lock of its own.
        self._last_beat = 0.0
        self._armed = False
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self.watchdog_tripped = False
        self._teardown_lock = threading.Lock()

    def heartbeat(self) -> None:
        """Called from the recorder's Tk main thread to prove it is still
        draining input. The first call arms the watchdog."""
        self._last_beat = time.monotonic()
        self._armed = True

    def _handle_physical_key(self, vk: int, message: int) -> Optional[str]:
        """Update modifier state and return a recorder emergency control."""
        is_down = message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
        is_up = message in (self.WM_KEYUP, self.WM_SYSKEYUP)
        if vk in self.CONTROL_VKS:
            if is_down:
                self._ctrl_down = True
            elif is_up:
                self._ctrl_down = False
            return None
        if vk in self.ALT_VKS:
            if is_down:
                self._alt_down = True
            elif is_up:
                self._alt_down = False
            return None
        if is_down and self._ctrl_down and self._alt_down:
            return self.CONTROL_TOKENS.get(vk)
        return None

    def run(self) -> None:
        if sys.platform != "win32":
            self.error = "physical-input isolation is Windows-only"
            self.ready.set()
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hook_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, hook_proc_type, wintypes.HINSTANCE, wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        @hook_proc_type
        def mouse_proc(code, w_param, l_param):
            if code < 0:
                return user32.CallNextHookEx(None, code, w_param, l_param)
            data = ctypes.cast(
                l_param, ctypes.POINTER(_MouseHookData),
            ).contents
            message = int(w_param)
            flags = int(data.flags)
            if _should_deliver_to_recorder(message, flags=flags):
                # Movement is passed through to the OS below, so the event's own
                # coordinates ARE the live cursor position — no anchor/delta
                # bookkeeping needed.
                self.mouse_sink(message, int(data.pt.x), int(data.pt.y))
            if not _should_swallow_physical_mouse(message, flags=flags):
                return user32.CallNextHookEx(None, code, w_param, l_param)
            return 1

        @hook_proc_type
        def keyboard_proc(code, w_param, l_param):
            if code < 0:
                return user32.CallNextHookEx(None, code, w_param, l_param)
            data = ctypes.cast(
                l_param, ctypes.POINTER(_KeyboardHookData),
            ).contents
            if int(data.flags) & self.LLKHF_INJECTED:
                return user32.CallNextHookEx(None, code, w_param, l_param)
            message = int(w_param)
            vk = int(data.vkCode)
            token = self._handle_physical_key(vk, message)
            if token:
                self.control_sink(token)
            # All physical keys are consumed; only XInput reaches the game.
            return 1

        self._mouse_proc = mouse_proc
        self._keyboard_proc = keyboard_proc
        self.thread_id = int(kernel32.GetCurrentThreadId())
        self._mouse_hook = user32.SetWindowsHookExW(
            self.WH_MOUSE_LL, mouse_proc, None, 0,
        )
        self._keyboard_hook = user32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL, keyboard_proc, None, 0,
        )
        if not self._mouse_hook or not self._keyboard_hook:
            self.error = (
                "failed to install low-level mouse/keyboard hooks "
                f"(winerr={ctypes.get_last_error()})"
            )
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
            self._mouse_hook = self._keyboard_hook = None
            self.ready.set()
            return

        self.active = True
        self.ready.set()
        self._start_watchdog(user32)
        msg = wintypes.MSG()
        try:
            # Blocking pump (not a PeekMessage poll): a low-level hook proc must
            # return inside LowLevelHooksTimeout (300 ms default) or Windows
            # silently drops the hook, so the pump thread can never sleep.
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            self._watchdog_stop.set()
            self._teardown(user32)

    # ---- teardown / watchdog ----

    def _teardown(self, user32) -> None:
        """Unhook. Idempotent and safe to call from either the pump thread or
        the watchdog thread."""
        with self._teardown_lock:
            self.active = False
            mouse_hook, self._mouse_hook = self._mouse_hook, None
            kbd_hook, self._keyboard_hook = self._keyboard_hook, None
            if mouse_hook:
                user32.UnhookWindowsHookEx(mouse_hook)
            if kbd_hook:
                user32.UnhookWindowsHookEx(kbd_hook)

    def _start_watchdog(self, user32) -> None:
        def _watch() -> None:
            while not self._watchdog_stop.wait(WATCHDOG_POLL_S):
                if not _watchdog_should_trip(
                    self._last_beat, time.monotonic(),
                    armed=self._armed, timeout_s=WATCHDOG_TIMEOUT_S,
                ):
                    continue
                self.watchdog_tripped = True
                print(
                    "[recorder] WATCHDOG: the recorder UI stopped responding "
                    f"for {WATCHDOG_TIMEOUT_S:.0f}s; releasing the keyboard/"
                    "mouse hooks so your input works again. This recording is "
                    "lost — kill the process and start over.",
                    file=sys.stderr,
                )
                # Ask the pump to exit first so it runs its own teardown; then
                # unhook directly in case the pump thread itself is wedged.
                self._post_quit()
                self._teardown(user32)
                return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=_watch, name="input-isolation-watchdog", daemon=True,
        )
        self._watchdog.start()

    def _post_quit(self) -> None:
        if sys.platform == "win32" and self.thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self.thread_id, self.WM_QUIT, 0, 0,
                )
            except Exception:
                pass

    def close(self) -> None:
        self._watchdog_stop.set()
        self._post_quit()
        if self.is_alive():
            self.join(timeout=1.0)
        # The pump normally tears down in its own finally; force it if the pump
        # never started or is stuck, so hooks never outlive close().
        if sys.platform == "win32" and (self._mouse_hook or self._keyboard_hook):
            self._teardown(ctypes.windll.user32)


def launch_and_wait(game_path: Path, cfg=None, *, n_pads: int = 1,
                    scenario: str | None = None):
    """Create the pads, spawn the game, wait for its window, FORCE foreground.

    Returns the pad driver, which the caller owns.

    The force-foreground loop is critical: without it the game launches in
    the background (Steam doesn't always promote it), the terminal stays
    foreground, and even after the recorder opens with WS_EX_NOACTIVATE
    the game never becomes foreground. Some games
    only poll XInput when they are the foreground window.

    The pads are created BEFORE the launch, and this function does not do that
    itself — `prelaunch.launch_with_pads` owns the whole ordering-sensitive
    sequence and passes the prepared virtual pads to the recorder worker.

    `cfg` is passed in by main(), which must load it before this runs (the pad
    count comes from it and the pads are created pre-launch). Re-reading the
    file here would risk the two copies disagreeing.
    """
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    if cfg is None:
        cfg = load_game_config(str(game_path))
    if cfg.auto_launch is None or not cfg.auto_launch.launch_path:
        sys.exit(
            f"ERROR: {game_path.name} has no auto_launch.launch_path; "
            "required for --auto-launch"
        )
    from coding4game_gui.benchmark.spec import WINDOWS_GAMES_DIR
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows.startup import enforce_live_window
    from coding4game_gui.benchmark.windows.startup import prepare_launch_state
    from coding4game_gui.benchmark.windows.prelaunch import launch_with_pads
    scenario_slug = scenario or game_path.stem
    scenario_path = WINDOWS_GAMES_DIR / f"{scenario_slug}.yaml"
    if scenario is not None and not scenario_path.is_file():
        sys.exit(f"ERROR: no benchmark scenario {scenario_slug!r}")
    if scenario_path.is_file():
        spec = load_game_spec(scenario_slug)
        if spec.get("driver_game") != game_path.stem:
            sys.exit(
                f"ERROR: scenario {scenario_slug!r} uses driver "
                f"{spec.get('driver_game')!r}; record against the driver slug")
        prepare_launch_state(spec, cfg)
    else:
        # Menu recording is part of driver onboarding and deliberately comes
        # before the benchmark contract may exist. Do not invent a no-save or
        # golden-restore contract here: kill only the residual game process,
        # then launch exactly what games/<driver>.yaml declares.
        from coding4game_gui.benchmark.windows import save_state as ss
        ss.kill_process(cfg.process)
        spec = None
        print(
            f"[recorder] no same-slug benchmark scenario for "
            f"{game_path.stem}; using driver-only launch "
            "(no save restore/cleanup or scenario window setup)",
            flush=True,
        )

    # Match the production launchers and auto-launch tests: find a visible,
    # titled window owned by the configured process.  Window titles are not a
    # stable identifier (Black Myth's b1-Win64-Shipping.exe is titled only
    # "b1"), so deriving search tokens from the EXE name can never be reliable.
    try:
        prepared = launch_with_pads(
            cfg.auto_launch, cfg.process,
            n_pads=n_pads,
            attempts=1,
            require_prepared_pads=True,
        )
    except RuntimeError as exc:
        # launch_with_pads has already closed the pads.
        sys.exit(f"ERROR: {exc}")
    driver = prepared.pads

    from coding4game_gui.benchmark.windows.auto_nav import wait_for_game_hwnd
    from coding4game_gui.benchmark.windows.prelaunch import _close_quietly
    try:
        hit = wait_for_game_hwnd(cfg.process, timeout=10.0)
        if hit is None:
            raise RuntimeError(f"could not locate the {cfg.process} HWND")
        if spec is not None:
            enforce_live_window(spec, cfg, hit[0])
        print(
            f"[recorder] window detected; settling "
            f"{cfg.auto_launch.settle_seconds:.0f}s "
            f"(forcing game to foreground every 2s)..."
        )
        slept = 0.0
        while slept < cfg.auto_launch.settle_seconds:
            _force_game_foreground(cfg.process)
            chunk = min(2.0, cfg.auto_launch.settle_seconds - slept)
            time.sleep(chunk)
            slept += chunk
    except BaseException:
        # The caller does not hold the driver yet, so nothing else would drop
        # these ViGEm devices.
        _close_quietly(driver)
        raise
    print(f"[recorder] ready. Game foregrounded; opening recorder next.")
    return driver


# --------------------- CLI ---------------------

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Record a mouse-clicked virtual-gamepad demo into a game YAML as PadActionSpec steps.",
    )
    p.add_argument("--game", required=True, help="Driver name (e.g. brotato) or YAML path")
    p.add_argument("--scenario", help="Cold-start scenario; its driver_game must match the selected driver.")
    p.add_argument("--auto-launch",
                   dest="auto_launch", action="store_true",
                   help="Spawn the game (via auto_launch.launch_path) + wait for its "
                        "window before opening the recorder.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print resulting YAML to stdout instead of patching the file")
    p.add_argument("--pads", type=int, choices=[1, 2], default=None,
                   help="Virtual pads to show. Defaults to the pads in the recorded menu sequence, or one.")
    p.add_argument("--debug", action="store_true",
                   help="Log recorder mouse events, the worker queue and game focus.")
    p.add_argument("--debounce-ms", type=int, default=120,
                   help="Ignore a press on the same button within this many ms of "
                        "the previous one (anti accidental double-click). "
                        "Default 120; pass 0 to disable.")
    return p


def main() -> int:
    # Standalone PowerShell/cmd sessions often default to GBK. Recorder status
    # messages contain Unicode symbols; replacement is preferable to crashing
    # after the operator has finished a long recording.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_argparser().parse_args()
    preflight_check_os()
    preflight_check_vgamepad_importable()

    # Apply debug flags BEFORE creating any window/worker.
    global DEBUG_UI, DEBUG_WORKER, DEBOUNCE_S
    if args.debug:
        DEBUG_UI = True
        DEBUG_WORKER = True
    DEBOUNCE_S = max(0.0, args.debounce_ms / 1000.0)

    game_path = resolve_game_path(args.game)
    print(f"[recorder] pre-flight OK. target={game_path.name}::menu_skip")

    # The config is read BEFORE the launch because n_pads — and therefore the
    # pads themselves — has to be known while the game is still closed.
    # Reading the YAML has no side effects, so hoisting it costs nothing.
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    _cfg = load_game_config(str(game_path))

    n_pads = args.pads or (1 + max((step.pad for step in _cfg.menu_skip), default=0))
    print(f"Recording {n_pads} pad(s).")

    # ---- virtual pads BEFORE the game launches ----
    # On the auto-launch path the pads are created INSIDE launch_and_wait, via
    # prelaunch.launch_with_pads, which owns the pads-then-launch ordering that
    # LibGDX games (Slay the Spire) depend on: they enumerate controllers once
    # at startup, and StS additionally persists the input mode it booted with,
    # so a pad that appears after launch is ignored for that session AND
    # inherited as disabled by the next. Building the driver here instead would
    # put this entry point back in charge of the ordering — the mistake that
    # left this script broken after the bench path was fixed.
    #
    # Without --auto-launch the game is already running (the user started it),
    # so its enumeration has happened and creating the pads here is all that is
    # available anyway.
    if args.auto_launch:
        driver = launch_and_wait(game_path, _cfg, n_pads=n_pads, scenario=args.scenario)
    else:
        driver = XInputDriver(n_pads=n_pads)
        driver.wakeup()
        time.sleep(1.0)

    # Final force-foreground right before the recorder window opens.
    # The recorder uses WS_EX_NOACTIVATE so it can't steal focus once open,
    # but it CAN'T grant focus to the game either — the game has to already
    # be foreground when the recorder appears.
    if sys.platform == "win32":
        ok = _force_game_foreground(_cfg.process)
        cursor_released = _release_cursor_clip()
        print(f"[recorder] game foreground before window open: {'YES' if ok else 'NO (game may not receive XInput!)'}")
        if DEBUG_UI or DEBUG_WORKER:
            print(f"[recorder] cursor clip released: {'YES' if cursor_released else 'NO'}")

    event_q: "queue.Queue[dict]" = queue.Queue()
    win = CaptureWindow(event_q, n_pads=n_pads)
    _release_cursor_clip()
    input_guard = PhysicalInputIsolation(
        win.queue_guarded_mouse,
        lambda token: event_q.put({
            "type": "control", "t": time.monotonic(), "token": token,
        }),
    )
    input_guard.start()
    input_guard.ready.wait(timeout=2.0)
    if not input_guard.active:
        error = input_guard.error or "input-isolation hook did not become ready"
        input_guard.close()
        _close_recorder_pads(driver)
        win.root.destroy()
        sys.exit(f"ERROR: recorder input isolation failed: {error}")
    win.heartbeat = input_guard.heartbeat
    win.show_escape_banner()
    worker = RecorderWorker(event_q, driver, win)
    worker.start()

    # Periodic re-foreground: every 2s, if game isn't foreground, force it.
    # Runs on the Tk main thread via root.after so it doesn't race with anything.
    def _keepalive_foreground():
        if sys.platform == "win32":
            try:
                user32 = ctypes.windll.user32
                fg = user32.GetForegroundWindow()
                # Cache the game hwnd lazily so we can tell if it IS foreground.
                if not hasattr(_keepalive_foreground, "_game_hwnd"):
                    import psutil as _ps
                    tpid = next(
                        (p.info["pid"] for p in _ps.process_iter(["name", "pid"])
                         if (p.info.get("name") or "").lower() == _cfg.process.lower()),
                        None,
                    )
                    if tpid:
                        found = []
                        def _cb(hwnd, _):
                            pid = ctypes.c_ulong()
                            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                            if pid.value == tpid and user32.IsWindowVisible(hwnd):
                                if user32.GetWindowTextLengthW(hwnd) > 0:
                                    found.append(hwnd)
                            return True
                        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                        user32.EnumWindows(EnumProc(_cb), 0)
                        _keepalive_foreground._game_hwnd = found[0] if found else None
                    else:
                        _keepalive_foreground._game_hwnd = None
                game_hwnd = _keepalive_foreground._game_hwnd

                if game_hwnd and fg != game_hwnd:
                    _force_game_foreground(_cfg.process)
                    if DEBUG_UI or DEBUG_WORKER:
                        new_fg = user32.GetForegroundWindow()
                        print(f"[fg]  was hwnd={fg}; re-forced game; now hwnd={new_fg} "
                              f"(game={game_hwnd}) -> {'OK' if new_fg == game_hwnd else 'STILL NOT'}")
                elif DEBUG_UI or DEBUG_WORKER:
                    # Quiet heartbeat: confirm game IS foreground every 2s.
                    print(f"[fg]  ok: fg=game hwnd={fg}")
                # Re-top the escape banner: forcing the game to foreground can
                # bury it, and a hidden banner is as bad as no banner.
                win.lift_escape_banner()
                # Reflect current foreground in the title bar so the user can SEE it.
                if game_hwnd:
                    status = "game-fg" if fg == game_hwnd else f"NOT-fg(hwnd={fg})"
                    win.root.title(f"Pad Recorder  [{status}]")
            except Exception:
                pass
            finally:
                # Foregrounding a UE game can make it re-apply ClipCursor.
                # Release on every keepalive tick so the mouse can still reach
                # this WS_EX_NOACTIVATE overlay without taking focus away from
                # the game.
                _release_cursor_clip()
        try:
            # 250 ms tick: most games only poll XInput while foreground, so a
            # 1 s gap was big enough for a click to land while the game was
            # briefly background (e.g. user dragged the recorder's title bar).
            win.root.after(250, _keepalive_foreground)
        except Exception:
            pass
    win.root.after(250, _keepalive_foreground)

    print("[recorder] virtual gamepad window open.")
    print("[recorder] left-click=press/hold, right-click=toggle-hold, RELEASE ALL clears stuck holds.")
    print("[recorder] D-PAD is digital; drag the LEFT STICK cap for true analog X/Y.")
    print("[recorder] release the stick to centre it; right-click a direction to lock/unlock it.")
    print("[recorder] when the playable scene is stable, stop touching the pad and press Ctrl+Alt+S.")
    print("[recorder] global controls: Ctrl+Alt+S=SAVE, Ctrl+Alt+Q=CANCEL, Ctrl+Alt+C=CHECKPOINT.")
    print(f"[recorder] SAVE / Ctrl+Alt+S → patches {game_path.name}::menu_skip")

    # Make sure vgamepad cleans up even on hard exit (Ctrl-C, uncaught exception).
    import atexit, signal
    atexit.register(_close_recorder_pads, driver)
    atexit.register(input_guard.close)

    def _sig_handler(signum, _frame):
        try:
            win.stop("signal")
        except Exception:
            pass
    try:
        signal.signal(signal.SIGINT, _sig_handler)
    except (ValueError, OSError):
        pass

    try:
        win.run()
    finally:
        input_guard.close()
        # Tell the worker to drain & exit; give it a moment.
        event_q.put({'type': 'system', 't': time.monotonic(), 'token': 'shutdown'})
        worker.join(timeout=2.0)
        _close_recorder_pads(driver)
        # Best effort for the TUI prompt that follows; a still-foreground game
        # may confine the cursor again after this process exits.
        _release_cursor_clip()

    if worker.stop_reason != "save":
        print(f"[recorder] cancelled (reason={worker.stop_reason!r}); no YAML written.")
        return 0

    steps = worker.tracker.steps
    if not steps:
        print("[recorder] no steps captured; nothing to save.")
        return 0

    if worker.tracker.overlap_detected:
        print(
            "[recorder] ⚠ overlap detected during recording — both pads held simultaneously."
            " The serialized YAML will not exactly reproduce that. Review the resulting YAML."
        )
    if worker.tracker.data_lost:
        print(
            "[recorder] ⚠ instantaneous / simultaneous events were coerced to MIN_EMITTED_DUR_S."
            " The recording is approximate. Replay and verify."
        )

    if args.dry_run:
        from ruamel.yaml import YAML
        y = YAML(typ='rt'); y.indent(mapping=2, sequence=4, offset=2)
        buf = io.StringIO()
        y.dump([s.to_yaml_dict() for s in steps], buf)
        print("---  recorded YAML (dry-run; not written)  ---")
        print(buf.getvalue())
        return 0

    patcher = YamlPatcher(game_path)
    try:
        result = patcher.patch(steps)
    except ValueError as e:
        print(f"[recorder] ERROR: step validation failed — {e}", file=sys.stderr)
        return 2
    print(f"[recorder] wrote {result['steps_written']} steps → {game_path}")
    if result['speed_entries_dropped'] > 0:
        print(
            f"[recorder] ⚠ {result['speed_entries_dropped']} existing step(s) had `speed:` "
            f"overrides — those were DROPPED (v1 doesn't capture xspeedhack speed)."
        )
    print(
        "[recorder] ⚠ The new menu_skip replaces comments inside the old list."
        " Review the YAML diff before committing."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
