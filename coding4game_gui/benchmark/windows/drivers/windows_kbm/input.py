"""Physical keyboard injection layered over the existing XInput driver.

The policy-facing key grammar is validated by ``windows.raw_actions``.  This
module owns only the OS adapter: KEY tokens become scan-code ``SendInput``
events, while every ordinary button/stick/trigger remains delegated to the
existing XInput driver unchanged.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from typing import Protocol


_KEY_TOKEN = re.compile(r"^KEY\[([^\]]+)\]$", re.IGNORECASE)
_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008

# Set-1 scan codes. DELETE, ALT and Windows keys are intentionally absent:
# those are harness/OS controls, not policy controls.
SCANCODES = {
    "ESC": 0x01,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "BACKSPACE": 0x0E, "TAB": 0x0F,
    "Q": 0x10, "W": 0x11, "E": 0x12, "R": 0x13, "T": 0x14,
    "Y": 0x15, "U": 0x16, "I": 0x17, "O": 0x18, "P": 0x19,
    "ENTER": 0x1C, "CTRL": 0x1D,
    "A": 0x1E, "S": 0x1F, "D": 0x20, "F": 0x21, "G": 0x22,
    "H": 0x23, "J": 0x24, "K": 0x25, "L": 0x26,
    "SHIFT": 0x2A,
    "Z": 0x2C, "X": 0x2D, "C": 0x2E, "V": 0x2F, "B": 0x30,
    "N": 0x31, "M": 0x32, "SPACE": 0x39,
    "UP": 0x48, "LEFT": 0x4B, "RIGHT": 0x4D, "DOWN": 0x50,
}
EXTENDED = frozenset({"UP", "DOWN", "LEFT", "RIGHT"})


class KeyboardSink(Protocol):
    def set_held(self, keys: list[str]) -> None: ...
    def release_all(self) -> None: ...
    def close(self) -> None: ...


def session_ok() -> bool:
    """Whether this process shares the active interactive desktop session."""
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    sid = ctypes.c_ulong()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
        return False
    return sid.value == kernel32.WTSGetActiveConsoleSessionId()


def _input_types():
    """Build the ABI structs lazily so pure tests import on non-Windows hosts."""
    ulong_ptr = wintypes.WPARAM

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr),
        ]

    class KeyboardInput(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ulong_ptr),
        ]

    class HardwareInput(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyboardInput),
                    ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]

    return Input, KeyboardInput


def type_startup_text(text: str, *, interval: float = 0.04,
                      send_input=None) -> None:
    """Deliver an ASCII menu query as Unicode packets, bypassing IME conversion."""
    if not text.isascii():
        raise ValueError("menu typing currently supports ASCII text only")
    input_type, keyboard_type = _input_types()
    if send_input is None:
        if not session_ok():
            raise RuntimeError("menu typing needs the active interactive session")
        if ctypes.sizeof(input_type) != 40:
            raise RuntimeError("menu typing requires the 64-bit Windows INPUT ABI")
        # A private function pointer keeps its INPUT type independent from
        # other keyboard/relative-mouse adapters' ctypes signatures.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send_input = user32.SendInput
        send_input.argtypes = (
            wintypes.UINT, ctypes.POINTER(input_type), ctypes.c_int)
        send_input.restype = wintypes.UINT
    for index, char in enumerate(text):
        events = (input_type * 2)()
        for event, flags in zip(events, (0x0004, 0x0006)):
            event.type = _INPUT_KEYBOARD
            event.ki = keyboard_type(wVk=0, wScan=ord(char), dwFlags=flags,
                                     time=0, dwExtraInfo=0)
        sent = send_input(2, events, ctypes.sizeof(input_type))
        if sent != 2:
            # A partial pair may have delivered key-down. Release it without
            # resending any characters or continuing a corrupted query.
            send_input(1, ctypes.byref(events[1]), ctypes.sizeof(input_type))
            raise OSError(f"menu text input inserted {sent}/2 events at index {index}")
        if interval > 0:
            time.sleep(interval)


class ScanCodeKeyboard:
    """Level-triggered keyboard state implemented with Win32 scan codes."""

    def __init__(self) -> None:
        if not session_ok():
            raise RuntimeError(
                "windows-kbm needs the active, unlocked interactive session")
        self._input_type, self._keyboard_input_type = _input_types()
        if ctypes.sizeof(self._input_type) != 40:
            raise RuntimeError(
                f"Win32 INPUT is {ctypes.sizeof(self._input_type)} bytes; "
                "expected 40 on 64-bit Windows")
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._user32.SendInput.argtypes = (
            wintypes.UINT, ctypes.POINTER(self._input_type), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT
        self._down: set[str] = set()

    def _send(self, name: str, *, up: bool) -> None:
        scan = SCANCODES[name]
        flags = _KEYEVENTF_SCANCODE
        if name in EXTENDED:
            flags |= _KEYEVENTF_EXTENDEDKEY
        if up:
            flags |= _KEYEVENTF_KEYUP
        event = self._input_type(type=_INPUT_KEYBOARD)
        event.ki = self._keyboard_input_type(
            wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        sent = self._user32.SendInput(
            1, ctypes.byref(event), ctypes.sizeof(self._input_type))
        if sent != 1:
            raise OSError(
                f"SendInput inserted {sent} events for {name} "
                f"({'up' if up else 'down'}); GetLastError="
                f"{self._kernel32.GetLastError()}")

    def set_held(self, keys: list[str]) -> None:
        wanted = set(keys)
        for name in sorted(self._down - wanted):
            try:
                self._send(name, up=True)
            finally:
                self._down.discard(name)
        for name in keys:
            if name not in self._down:
                self._send(name, up=False)
                self._down.add(name)

    def release_all(self) -> None:
        first_error: Exception | None = None
        for name in sorted(self._down):
            try:
                self._send(name, up=True)
            except Exception as exc:  # keep releasing every other key
                first_error = first_error or exc
            finally:
                self._down.discard(name)
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        self.release_all()


class KeyboardMouseInputDriver:
    """Add raw keyboard keys to an existing XInput driver without replacing it."""

    def __init__(self, gamepad, keyboard: KeyboardSink) -> None:
        self._gamepad = gamepad
        self._keyboard = keyboard
        self.n_pads = gamepad.n_pads

    @staticmethod
    def _split_buttons(buttons) -> tuple[list[str], list[str]]:
        pad: list[str] = []
        keys: list[str] = []
        for raw in buttons or []:
            token = str(raw).upper()
            match = _KEY_TOKEN.fullmatch(token)
            if match:
                keys.append(match.group(1).upper())
            else:
                pad.append(token)
        return pad, keys

    def apply_state(self, idx: int, *, buttons, left_stick=(0.0, 0.0),
                    right_stick=(0.0, 0.0), left_trigger=0.0,
                    right_trigger=0.0) -> None:
        pad, keys = self._split_buttons(buttons)
        if keys and idx != 0:
            raise ValueError("raw keyboard controls are available only to player 1")
        self._gamepad.apply_state(
            idx, buttons=pad, left_stick=left_stick,
            right_stick=right_stick, left_trigger=left_trigger,
            right_trigger=right_trigger)
        if idx == 0:
            try:
                self._keyboard.set_held(keys)
            except BaseException:
                self._gamepad.reset_pad(idx)
                raise

    def reset_pad(self, idx: int) -> None:
        try:
            if idx == 0:
                self._keyboard.release_all()
        finally:
            self._gamepad.reset_pad(idx)

    def reset(self) -> None:
        try:
            self._keyboard.release_all()
        finally:
            self._gamepad.reset()

    def wakeup(self, duration: float = 0.1) -> None:
        self._gamepad.wakeup(duration)

    def close(self) -> None:
        try:
            self._keyboard.close()
        finally:
            self._gamepad.close()
