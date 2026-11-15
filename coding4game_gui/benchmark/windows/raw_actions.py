"""Parsing, prompt selection, and resolution of raw Windows input actions.

The stepped wire format accepts commands such as
``ACT 3 A,LS[0.1,0.1],RT[0.5]``.  Named digital actions continue to come from
the game's ``ActionSpec`` table. The runtime input mode exposes either that
gamepad surface or a keyboard/mouse-only surface, never both to one policy.
"""
from __future__ import annotations

import math
from coding4game_gui.benchmark.windows.timing import validate_action_tics
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from coding4game_gui.benchmark.policy_observation import POLICY_HEIGHT
from coding4game_gui.benchmark.policy_observation import POLICY_WIDTH
from coding4game_gui.benchmark.windows.games_config import ActionSpec
from coding4game_gui.benchmark.windows.verbs import combine_states


WINDOWS_INPUT_GAMEPAD = "gamepad"
WINDOWS_INPUT_GAMEPAD_KBM = "gamepad-kbm"
WINDOWS_INPUT_MODES = frozenset({
    WINDOWS_INPUT_GAMEPAD,
    WINDOWS_INPUT_GAMEPAD_KBM,
})
MAX_SCROLL_STEPS = 10
MAX_RELATIVE_MOUSE_DELTA = 1000

# Keep OS-global and harness-owned keys out of policy reach.  This is a
# physical-key whitelist, not a game-semantic mapping.
WINDOWS_KBM_KEYS = (
    "UP", "DOWN", "LEFT", "RIGHT",
    "ENTER", "BACKSPACE", "SPACE", "TAB", "ESC",
    "SHIFT", "CTRL",
    *tuple(chr(code) for code in range(ord("A"), ord("Z") + 1)),
    *tuple(str(value) for value in range(10)),
)


WINDOWS_RAW_ACTION_PROTOCOL = """Raw Windows gamepad action protocol:
Each action must be a complete line in the form `ACT <tics> <control[,control...]>`.
`<tics>` must be an integer from 1 to 1000; remaining run/episode time may shorten it.
Usage is cumulative running seconds times the tic rate, rounded to the nearest
integer with halves up, not rounded per action. Timing uses host pause boundaries,
not an internal engine clock.
The last executed action may overrun the limit by up to two tics without invalidating
the episode. Actual usage is retained; this never permits an additional action.
The per-action cap is 1000 tics; larger requests are rejected. Scores remain null during gameplay and settle
after the episode ends. Registered terminal screens are checked only after each action finishes; inactivity is not terminal.
Fixed digital controls come from the game's action
table (for example `A`, `LB`, or `NEUTRAL`). Analog controls carry values inline:
`LS[x,y]` and `RS[x,y]` use x/y in [-1,1], while `LT[v]` and `RT[v]` use v in
[0,1]. Examples: `ACT 3 LS[0.1,0.1]`, `ACT 5 A,LS[-0.7,0.4],RT[0.5]`,
and `ACT 2 NEUTRAL`. Every ACT is one complete pulse: the listed state is held
for the requested tics and then all controls are released before the next ACT.
Repeated identical ACT lines are therefore repeated presses, not one continuous
hold. Use NEUTRAL to advance game time without input; it must not be combined
with another control.
"""

WINDOWS_KBM_ACTION_PROTOCOL = """Raw Windows keyboard/mouse action protocol:
Each action must be a complete line in the form `ACT <tics> <control[,control...]>`.
`<tics>` must be an integer from 1 to 1000; remaining run/episode time may shorten it.
Usage is cumulative running seconds times the tic rate, rounded to the nearest
integer with halves up, not rounded per action. Timing uses host pause boundaries,
not an internal engine clock.
The last executed action may overrun the limit by up to two tics without invalidating
the episode. Actual usage is retained; this never permits an additional action.
The per-action cap is 1000 tics; larger requests are rejected. Scores remain null during gameplay and settle
after the episode ends. Registered terminal screens are checked only after each action finishes; inactivity is not terminal.
Use
`KEY[<key>]` for a physical keyboard key, `MOVE[x,y]` to move and hover the
pointer, `RMOVE[dx,dy]` for one relative mouse movement used by captured-camera
games, `CLICK[x,y]` for one left click, `CLICK_CURRENT` for a left click
without moving a captured pointer, `RCLICK[x,y]` for one right click,
`MCLICK[x,y]` for one middle click, and `DOUBLE_CLICK[x,y]` for one left
double-click. `DRAG[x1,y1,x2,y2]`
holds the left button while moving in a straight line from start to end over
the complete ACT duration. `SCROLL[x,y,dx,dy]` moves to a point and emits
horizontal/vertical wheel steps there; dx/dy are integers in
[-{max_scroll},{max_scroll}], positive dx scrolls right, positive dy scrolls
up, and at least one must be non-zero. Pointer actions use the exact
{width}x{height} policy screenshot pixel space: X is an integer in 0..{x_max}
from left to right and Y is an integer in 0..{y_max} from top to bottom. For
example `MOVE[{center_x},{center_y}]` hovers at the centre and
`CLICK[{center_x},{center_y}]` clicks it. Read coordinates directly from the
screenshot; do not normalise them. MOVE leaves the pointer at its target for
the complete ACT so the next screenshot can show hover-dependent UI. DRAG and
SCROLL leave it at the final/target point. All click actions release the button
and leave the pointer at the clicked target. Use MOVE explicitly to move away.
RMOVE deltas are signed integer mouse-device units in
[-{max_relative_mouse_delta},{max_relative_mouse_delta}], are not screenshot
coordinates, and cannot both be zero. One relative event is injected near the
start of the ACT; the remaining tics let the game render its response.
Allowed keys: {keys}. Keys listed together are held simultaneously for the ACT tics,
except ESC, which must be used alone. ESCAPE is an alias for ESC; use it for
game-native back/cancel/pause menus, not to control the host freeze mechanism.
Keep every pointer action in its own ACT; combining one with another
control is rejected because their ordering would otherwise be ambiguous.
Invalid or out-of-range controls reject the complete reply before any action
executes.
Every ACT is one complete pulse: held keys are released before the next ACT.
Use `ACT <tics> NEUTRAL` to advance game time without input. Other input-device
buttons, sticks, and triggers are unavailable in keyboard/mouse mode.
"""

_INPUT_GUIDE_MARKER = re.compile(
    r"^\s*<!--\s*C4G_WINDOWS_INPUT:(gamepad|gamepad-kbm):(begin|end)\s*-->\s*$",
    re.IGNORECASE,
)


_TOKEN = (
    r"(?:[A-Za-z][A-Za-z0-9_]*|"
    r"(?:LS|RS|CLICK|RCLICK|MCLICK|DOUBLE_CLICK|MOVE|RMOVE|DRAG|SCROLL)\[[^\]]*\]|"
    r"(?:LT|RT|KEY)\[[^\]]*\])"
)
_ACT_PREFIX = r"[ \t>*\-.`\)\d]*"
_ACT_RE = re.compile(
    rf"^{_ACT_PREFIX}ACT\s+(-?\d+)\s+({_TOKEN}(?:\s*,\s*{_TOKEN})*)"
    r"[ \t`*).,]*$",
    re.IGNORECASE,
)
_ACT_HEAD_RE = re.compile(
    rf"^{_ACT_PREFIX}ACT\s+(-?\d+)(?:\s+(.*))?$", re.IGNORECASE
)
_DYNAMIC_RE = re.compile(r"^(LS|RS)\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_TRIGGER_RE = re.compile(r"^(LT|RT)\[([^\]]+)\]$", re.IGNORECASE)
_CLICK_RE = re.compile(r"^CLICK\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_RCLICK_RE = re.compile(r"^RCLICK\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_MCLICK_RE = re.compile(r"^MCLICK\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_DOUBLE_CLICK_RE = re.compile(
    r"^DOUBLE_CLICK\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_MOVE_RE = re.compile(r"^MOVE\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_RELATIVE_MOVE_RE = re.compile(
    r"^RMOVE\[([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_DRAG_RE = re.compile(
    r"^DRAG\[([^,\]]+),([^,\]]+),([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_SCROLL_RE = re.compile(
    r"^SCROLL\[([^,\]]+),([^,\]]+),([^,\]]+),([^\]]+)\]$", re.IGNORECASE)
_KEY_RE = re.compile(r"^KEY\[([^\]]+)\]$", re.IGNORECASE)


def normalize_windows_input_mode(value: str | None) -> str:
    mode = str(value or WINDOWS_INPUT_GAMEPAD).strip().lower()
    if mode not in WINDOWS_INPUT_MODES:
        raise ValueError(
            f"unknown Windows input mode {mode!r}; "
            f"expected one of {sorted(WINDOWS_INPUT_MODES)}"
        )
    return mode


def windows_action_protocol(input_mode: str | None = None) -> str:
    """Render the protocol for one explicitly selected runtime input mode."""
    mode = normalize_windows_input_mode(input_mode)
    if mode == WINDOWS_INPUT_GAMEPAD:
        return WINDOWS_RAW_ACTION_PROTOCOL
    return WINDOWS_KBM_ACTION_PROTOCOL.format(
        width=POLICY_WIDTH, height=POLICY_HEIGHT,
        x_max=POLICY_WIDTH - 1, y_max=POLICY_HEIGHT - 1,
        center_x=POLICY_WIDTH // 2, center_y=POLICY_HEIGHT // 2,
        max_scroll=MAX_SCROLL_STEPS,
        max_relative_mouse_delta=MAX_RELATIVE_MOUSE_DELTA,
        keys=", ".join(WINDOWS_KBM_KEYS))


def windows_code_action_protocol(input_mode: str | None = None) -> str:
    """Policy-return variant of the selected raw input protocol."""
    mode = normalize_windows_input_mode(input_mode)
    if mode == WINDOWS_INPUT_GAMEPAD:
        return """Raw Windows gamepad policy controls:
`buttons` may include parameterized physical controls: `LS[x,y]` or `RS[x,y]`
with each axis in `[-1,1]`, and `LT[v]` or `RT[v]` with `v` in `[0,1]`.
These values are held exactly for the returned duration and can be combined
with digital buttons, e.g. `return (["LS[0.1,0.1]"], 3)`."""
    return """Raw Windows keyboard/mouse policy controls:
`buttons` may contain `NEUTRAL`, `KEY[<key>]`, `MOVE[x,y]`, `RMOVE[dx,dy]`, `CLICK[x,y]`, `CLICK_CURRENT`,
`RCLICK[x,y]`, `MCLICK[x,y]`, `DOUBLE_CLICK[x,y]`,
`DRAG[x1,y1,x2,y2]`, or
`SCROLL[x,y,dx,dy]`.
Pointer coordinates are pixels in the exact {width}x{height} policy frame: X is
in `0..{x_max}` and Y is in `0..{y_max}`. Read them directly from `frame`; do
not normalise them. For example `return (["MOVE[{center_x},{center_y}]"], 5)`
moves there and holds the hover for 5 tics, while
`return (["CLICK[{center_x},{center_y}]"], 1)` left-clicks the centre.
`RMOVE[dx,dy]` emits one relative mouse movement for camera control; its
signed integer deltas are in [-{max_relative_mouse_delta},{max_relative_mouse_delta}],
are not screenshot coordinates, and cannot both be zero.
DRAG uses the returned duration for its complete straight-line gesture.
SCROLL dx/dy are integer wheel steps in [-{max_scroll},{max_scroll}], where
positive dx moves right and positive dy moves up.
Allowed keys: {keys}.
ESC (alias ESCAPE) must be used alone for game-native back/cancel/pause menus.
Multiple keys may be held together, e.g. `return (["KEY[W]", "KEY[D]"], 3)`.
Keep every pointer action in its own return. Other input-device buttons,
sticks, and triggers are unavailable in keyboard/mouse mode.""".format(
        width=POLICY_WIDTH, height=POLICY_HEIGHT,
        x_max=POLICY_WIDTH - 1, y_max=POLICY_HEIGHT - 1,
        center_x=POLICY_WIDTH // 2, center_y=POLICY_HEIGHT // 2,
        max_scroll=MAX_SCROLL_STEPS,
        max_relative_mouse_delta=MAX_RELATIVE_MOUSE_DELTA,
        keys=", ".join(WINDOWS_KBM_KEYS))


def windows_input_protocol_addendum(input_mode: str | None = None) -> str:
    """Compatibility alias for callers migrating to the full protocol."""
    return windows_action_protocol(input_mode).strip()


def select_windows_gameplay(
    gameplay: str, input_mode: str | None = None, *, scenario: str = "Windows game"
) -> str:
    """Select the prompt-facing control guide for one policy input mode.

    The markers live inside the existing ``gameplay`` scalar, so semantic game
    knowledge stays in the scenario YAML without adding a top-level selector.
    Unmarked legacy scenarios remain valid for the default gamepad mode; they
    fail closed if keyboard/mouse is requested rather than showing a wrong
    controller guide.
    """
    mode = normalize_windows_input_mode(input_mode)
    selected: list[str] = []
    active: str | None = None
    seen: set[str] = set()
    for line in str(gameplay or "").splitlines():
        marker = _INPUT_GUIDE_MARKER.fullmatch(line)
        if marker:
            marked_mode, edge = marker.group(1).lower(), marker.group(2).lower()
            if edge == "begin":
                if active is not None:
                    raise ValueError(
                        f"{scenario}: nested Windows input guide markers")
                active = marked_mode
                seen.add(marked_mode)
            else:
                if active != marked_mode:
                    raise ValueError(
                        f"{scenario}: mismatched Windows input guide marker")
                active = None
            continue
        if active is None or active == mode:
            selected.append(line)
    if active is not None:
        raise ValueError(f"{scenario}: unclosed Windows input guide marker")
    if mode == WINDOWS_INPUT_GAMEPAD_KBM and mode not in seen:
        raise ValueError(
            f"{scenario}: keyboard/mouse mode has no verified gameplay guide")
    return "\n".join(selected).strip()


def windows_kbm_catalog_entries(input_mode: str | None = None) -> list[dict]:
    """Dynamic controls used by the keyboard/mouse-only action catalog."""
    if normalize_windows_input_mode(input_mode) == WINDOWS_INPUT_GAMEPAD:
        return []
    return [
        {
            "name": "KEY[<KEY>]",
            "description": (
                "Hold a whitelisted physical keyboard key for the ACT tics; "
                "multiple KEY controls may be combined."),
        },
        {
            "name": "MOVE[<X>,<Y>]",
            "description": (
                f"Move to and hover at a policy screenshot pixel for the "
                f"ACT tics; X is 0..{POLICY_WIDTH - 1} and Y is 0.."
                f"{POLICY_HEIGHT - 1}."),
        },
        {
            "name": "RMOVE[<DX>,<DY>]",
            "description": (
                "Inject one relative mouse movement for captured-camera games; "
                f"DX and DY are device units in [-{MAX_RELATIVE_MOUSE_DELTA},"
                f"{MAX_RELATIVE_MOUSE_DELTA}] and cannot both be zero."),
        },
        {
            "name": "CLICK[<X>,<Y>]",
            "description": (
                f"Left-click a policy screenshot pixel; X is 0.."
                f"{POLICY_WIDTH - 1} and Y is 0..{POLICY_HEIGHT - 1}."),
        },
        {
            "name": "CLICK_CURRENT",
            "description": "Left-click without moving a captured game pointer.",
        },
        {
            "name": "RCLICK[<X>,<Y>]",
            "description": (
                f"Right-click a policy screenshot pixel; X is 0.."
                f"{POLICY_WIDTH - 1} and Y is 0..{POLICY_HEIGHT - 1}."),
        },
        {
            "name": "MCLICK[<X>,<Y>]",
            "description": (
                f"Middle-click a policy screenshot pixel; X is 0.."
                f"{POLICY_WIDTH - 1} and Y is 0..{POLICY_HEIGHT - 1}."),
        },
        {
            "name": "DOUBLE_CLICK[<X>,<Y>]",
            "description": (
                f"Left double-click a policy screenshot pixel; X is 0.."
                f"{POLICY_WIDTH - 1} and Y is 0..{POLICY_HEIGHT - 1}."),
        },
        {
            "name": "DRAG[<X1>,<Y1>,<X2>,<Y2>]",
            "description": (
                "Hold left while moving in a straight line between two "
                "policy screenshot pixels over the ACT tics."),
        },
        {
            "name": "SCROLL[<X>,<Y>,<DX>,<DY>]",
            "description": (
                f"Move to a policy screenshot pixel and scroll by integer "
                f"wheel steps in [-{MAX_SCROLL_STEPS},{MAX_SCROLL_STEPS}]; "
                "positive DX is right and positive DY is up."),
        },
    ]


def parse_windows_acts(
    reply: str, max_burst: int | None
) -> list[tuple[list[str], int]]:
    """Parse complete ACT lines, preserving dynamic token text.

    Markdown bullets and the punctuation emitted by the existing protocol are
    accepted.  An ``ACT`` mentioned in prose is not a line and is ignored.
    Tic counts are integers from 1 to 1000; oversized actions are rejected.
    """
    if not isinstance(reply, str):
        return []
    if max_burst is not None and max_burst <= 0:
        return []
    acts: list[tuple[list[str], int]] = []
    for line in reply.splitlines():
        candidate = line.strip()
        match = _ACT_RE.match(candidate)
        if not match:
            head = _ACT_HEAD_RE.match(candidate)
            if head:
                raise ValueError(f"invalid raw ACT syntax: {candidate}")
            continue
        tics = int(match.group(1))
        validate_action_tics(tics)
        tokens = [part.strip().upper() for part in _split_tokens(match.group(2))]
        for token in tokens:
            if ("[" in token or "]" in token) and not any(pattern.fullmatch(token)
                    for pattern in (
                        _DYNAMIC_RE, _TRIGGER_RE, _CLICK_RE, _RCLICK_RE,
                        _MCLICK_RE, _DOUBLE_CLICK_RE, _MOVE_RE,
                        _RELATIVE_MOVE_RE, _DRAG_RE,
                        _SCROLL_RE,
                        _KEY_RE,
                    )):
                raise ValueError(f"invalid raw ACT syntax: {candidate}")
        if tokens:
            acts.append((tokens, tics))
    if max_burst is not None and len(acts) > max_burst:
        raise ValueError(
            "ACT burst limit exceeded: "
            f"received {len(acts)} parseable ACT lines, cap is {max_burst}"
        )
    return acts


def _split_tokens(blob: str) -> list[str]:
    """Split commas only outside an analog vector/trigger bracket pair."""
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(blob):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(blob[start:index].strip())
            start = index + 1
    result.append(blob[start:].strip())
    return result


def _zero_state() -> dict[str, Any]:
    return {
        "buttons": [],
        "left_stick": (0.0, 0.0),
        "right_stick": (0.0, 0.0),
        "left_trigger": 0.0,
        "right_trigger": 0.0,
    }


def _fixed_table(fixed_actions: Mapping[str, ActionSpec]) -> dict[str, ActionSpec]:
    return {str(name).upper(): spec for name, spec in fixed_actions.items()}


def _number(raw: str, label: str) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid raw action {label}: expected a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"invalid raw action {label}: value must be finite")
    return value


@dataclass(frozen=True)
class ResolvedRawAction:
    """One validated ACT split into held state and positional pointer work."""

    state: dict[str, Any]
    clicks: tuple[tuple[float, float], ...] = ()
    right_clicks: tuple[tuple[float, float], ...] = ()
    middle_clicks: tuple[tuple[float, float], ...] = ()
    double_clicks: tuple[tuple[float, float], ...] = ()
    moves: tuple[tuple[float, float], ...] = ()
    relative_moves: tuple[tuple[int, int], ...] = ()
    click_current: bool = False
    drags: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = ()
    scrolls: tuple[tuple[tuple[float, float], int, int], ...] = ()


def _resolve_pointer_coords(raw_x: str, raw_y: str, kind: str,
                            content_size: tuple[int, int] | None
                            ) -> tuple[float, float]:
    """Validate one policy-frame point and map it to calibrated game space."""
    if content_size is None:
        raise ValueError(f"invalid raw action: {kind} needs a frame size")
    raw_x, raw_y = raw_x.strip(), raw_y.strip()
    try:
        x, y = int(raw_x), int(raw_y)
    except ValueError as exc:
        raise ValueError(
            f"invalid raw action {kind}: coordinates must be integers") from exc
    if str(x) != raw_x or str(y) != raw_y:
        raise ValueError(
            f"invalid raw action {kind}: coordinates must be canonical integers")
    if not 0 <= x < POLICY_WIDTH:
        raise ValueError(
            f"invalid raw action {kind}: X must be in [0, {POLICY_WIDTH - 1}]")
    if not 0 <= y < POLICY_HEIGHT:
        raise ValueError(
            f"invalid raw action {kind}: Y must be in [0, {POLICY_HEIGHT - 1}]")
    width, height = int(content_size[0]), int(content_size[1])
    if width <= 0 or height <= 0:
        raise ValueError(
            f"invalid raw action {kind}: frame size must be positive")
    return (
        float(round(x * (width - 1) / (POLICY_WIDTH - 1))),
        float(round(y * (height - 1) / (POLICY_HEIGHT - 1))),
    )


def _resolve_pointer_point(match: re.Match, kind: str,
                           content_size: tuple[int, int] | None
                           ) -> tuple[float, float]:
    return _resolve_pointer_coords(
        match.group(1), match.group(2), kind, content_size)


def _canonical_int(raw: str, kind: str, field: str) -> int:
    value = raw.strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(
            f"invalid raw action {kind}: {field} must be an integer") from exc
    if str(parsed) != value:
        raise ValueError(
            f"invalid raw action {kind}: {field} must be a canonical integer")
    return parsed


def resolve_raw_action(
    tokens: list[str] | tuple[str, ...],
    fixed_actions: Mapping[str, ActionSpec],
    *,
    input_mode: str | None = None,
    content_size: tuple[int, int] | None = None,
) -> ResolvedRawAction:
    """Resolve and validate one gamepad or keyboard/mouse-only ACT.

    This is the transaction-validation seam used by both ``validate_action_plan``
    and live execution.  Pointer coordinates are converted into the same
    calibrated game-space pixels consumed by the existing mouse driver.
    """
    mode = normalize_windows_input_mode(input_mode)
    if not isinstance(tokens, (list, tuple)) or not tokens:
        raise ValueError("invalid raw action: token list must not be empty")
    normalized = [str(token).strip().upper() for token in tokens]
    if any(not token for token in normalized):
        raise ValueError("invalid raw action: empty token")
    if "NEUTRAL" in normalized and len(normalized) != 1:
        raise ValueError("invalid raw action: NEUTRAL cannot be mixed with controls")

    gamepad_tokens: list[str] = []
    key_tokens: list[str] = []
    clicks: list[tuple[float, float]] = []
    right_clicks: list[tuple[float, float]] = []
    middle_clicks: list[tuple[float, float]] = []
    double_clicks: list[tuple[float, float]] = []
    moves: list[tuple[float, float]] = []
    relative_moves: list[tuple[int, int]] = []
    drags: list[tuple[tuple[float, float], tuple[float, float]]] = []
    scrolls: list[tuple[tuple[float, float], int, int]] = []
    pointer_kind: str | None = None
    for token in normalized:
        if token == "CLICK_CURRENT":
            if mode != WINDOWS_INPUT_GAMEPAD_KBM:
                raise ValueError(
                    "invalid raw action: CLICK_CURRENT requires keyboard/mouse mode")
            if pointer_kind is not None:
                raise ValueError(
                    "invalid raw action: pointer action must be the only control in its ACT")
            pointer_kind = "CLICK_CURRENT"
            continue
        click = _CLICK_RE.fullmatch(token)
        right_click = _RCLICK_RE.fullmatch(token)
        middle_click = _MCLICK_RE.fullmatch(token)
        double_click = _DOUBLE_CLICK_RE.fullmatch(token)
        move = _MOVE_RE.fullmatch(token)
        relative_move = _RELATIVE_MOVE_RE.fullmatch(token)
        drag = _DRAG_RE.fullmatch(token)
        scroll = _SCROLL_RE.fullmatch(token)
        key = _KEY_RE.fullmatch(token)
        pointer_match = (
            click or right_click or middle_click or double_click or move or
            relative_move or drag or scroll)
        if pointer_match:
            kind = (
                "CLICK" if click else
                "RCLICK" if right_click else
                "MCLICK" if middle_click else
                "DOUBLE_CLICK" if double_click else
                "MOVE" if move else
                "RMOVE" if relative_move else
                "DRAG" if drag else "SCROLL"
            )
            if mode != WINDOWS_INPUT_GAMEPAD_KBM:
                raise ValueError(
                    f"invalid raw action: {kind} requires "
                    "--windows-input gamepad-kbm")
            if pointer_kind is not None:
                raise ValueError(
                    "invalid raw action: pointer action must be the only "
                    "control in its ACT")
            pointer_kind = kind
            if relative_move:
                dx = _canonical_int(relative_move.group(1), kind, "DX")
                dy = _canonical_int(relative_move.group(2), kind, "DY")
                if not (-MAX_RELATIVE_MOUSE_DELTA <= dx <= MAX_RELATIVE_MOUSE_DELTA and
                        -MAX_RELATIVE_MOUSE_DELTA <= dy <= MAX_RELATIVE_MOUSE_DELTA):
                    raise ValueError(
                        f"invalid raw action {kind}: DX and DY must be in "
                        f"[-{MAX_RELATIVE_MOUSE_DELTA}, "
                        f"{MAX_RELATIVE_MOUSE_DELTA}]")
                if dx == 0 and dy == 0:
                    raise ValueError(
                        f"invalid raw action {kind}: DX and DY cannot both be zero")
                relative_moves.append((dx, dy))
            elif drag:
                start = _resolve_pointer_coords(
                    drag.group(1), drag.group(2), kind, content_size)
                end = _resolve_pointer_coords(
                    drag.group(3), drag.group(4), kind, content_size)
                drags.append((start, end))
            elif scroll:
                point = _resolve_pointer_coords(
                    scroll.group(1), scroll.group(2), kind, content_size)
                dx = _canonical_int(scroll.group(3), kind, "DX")
                dy = _canonical_int(scroll.group(4), kind, "DY")
                if not (-MAX_SCROLL_STEPS <= dx <= MAX_SCROLL_STEPS and
                        -MAX_SCROLL_STEPS <= dy <= MAX_SCROLL_STEPS):
                    raise ValueError(
                        f"invalid raw action {kind}: DX and DY must be in "
                        f"[-{MAX_SCROLL_STEPS}, {MAX_SCROLL_STEPS}]")
                if dx == 0 and dy == 0:
                    raise ValueError(
                        f"invalid raw action {kind}: DX and DY cannot both be zero")
                scrolls.append((point, dx, dy))
            else:
                point = _resolve_pointer_point(
                    click or right_click or middle_click or double_click or move,
                    kind, content_size)
                target = (
                    clicks if click else
                    right_clicks if right_click else
                    middle_clicks if middle_click else
                    double_clicks if double_click else moves
                )
                target.append(point)
            continue
        if key:
            if mode != WINDOWS_INPUT_GAMEPAD_KBM:
                raise ValueError(
                    "invalid raw action: KEY requires --windows-input gamepad-kbm")
            name = key.group(1).strip().upper()
            if name == "ESCAPE":
                name = "ESC"
            if name not in WINDOWS_KBM_KEYS:
                raise ValueError(
                    f"invalid raw action KEY: {name!r} is not allowed")
            rendered = f"KEY[{name}]"
            if rendered in key_tokens:
                raise ValueError(f"invalid raw action: duplicate keyboard key {name}")
            key_tokens.append(rendered)
            continue
        gamepad_tokens.append(token)

    if "KEY[ESC]" in key_tokens and len(tokens) != 1:
        raise ValueError("invalid raw action: ESC must be the only control in its ACT")
    if mode == WINDOWS_INPUT_GAMEPAD_KBM:
        disallowed = [token for token in gamepad_tokens if token != "NEUTRAL"]
        if disallowed:
            raise ValueError(
                "invalid raw action: keyboard/mouse mode does not allow "
                f"gamepad control {disallowed[0]}")
    state = (resolve_raw_state(gamepad_tokens, fixed_actions)
             if gamepad_tokens else _zero_state())
    if pointer_kind is not None and (key_tokens or gamepad_tokens):
        raise ValueError(
            f"invalid raw action: {pointer_kind} must be the only control in its ACT")
    state["buttons"] = list(state["buttons"]) + key_tokens
    return ResolvedRawAction(
        state=state,
        clicks=tuple(clicks),
        right_clicks=tuple(right_clicks),
        middle_clicks=tuple(middle_clicks),
        double_clicks=tuple(double_clicks),
        moves=tuple(moves),
        relative_moves=tuple(relative_moves),
        click_current=pointer_kind == "CLICK_CURRENT",
        drags=tuple(drags),
        scrolls=tuple(scrolls),
    )


def resolve_raw_state(
    tokens: list[str] | tuple[str, ...],
    fixed_actions: Mapping[str, ActionSpec],
) -> dict[str, Any]:
    """Resolve one ACT token list to the driver's complete pad-state mapping."""
    if not isinstance(tokens, (list, tuple)) or not tokens:
        raise ValueError("invalid raw action: token list must not be empty")
    table = _fixed_table(fixed_actions)
    normalized = [str(token).strip().upper() for token in tokens]
    if any(not token for token in normalized):
        raise ValueError("invalid raw action: empty token")
    if "NEUTRAL" in normalized:
        if len(normalized) != 1:
            raise ValueError("invalid raw action: NEUTRAL cannot be mixed with controls")
        if "NEUTRAL" in table:
            return combine_states([table["NEUTRAL"]])
        return _zero_state()

    specs: list[ActionSpec] = []
    dynamic_channels: set[str] = set()
    for token in normalized:
        dynamic = _DYNAMIC_RE.fullmatch(token)
        trigger = _TRIGGER_RE.fullmatch(token)
        if dynamic:
            channel = dynamic.group(1).upper()
            if channel in dynamic_channels:
                raise ValueError(f"invalid raw action: duplicate dynamic channel {channel}")
            x = _number(dynamic.group(2), f"{channel}[x]")
            y = _number(dynamic.group(3), f"{channel}[y]")
            if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
                raise ValueError(f"invalid raw action {channel}: vector values must be in [-1, 1]")
            dynamic_channels.add(channel)
            specs.append(
                ActionSpec(
                    name=channel,
                    description="inline raw analog stick",
                    left_stick=(x, y) if channel == "LS" else (0.0, 0.0),
                    right_stick=(x, y) if channel == "RS" else (0.0, 0.0),
                )
            )
            continue
        if trigger:
            channel = trigger.group(1).upper()
            if channel in dynamic_channels:
                raise ValueError(f"invalid raw action: duplicate dynamic channel {channel}")
            value = _number(trigger.group(2), channel)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"invalid raw action {channel}: value must be in [0, 1]")
            dynamic_channels.add(channel)
            specs.append(
                ActionSpec(
                    name=channel,
                    description="inline raw analog trigger",
                    left_trigger=value if channel == "LT" else 0.0,
                    right_trigger=value if channel == "RT" else 0.0,
                )
            )
            continue
        if token not in table:
            raise ValueError(f"invalid raw action: unknown fixed token {token}")
        specs.append(table[token])
    return combine_states(specs)
