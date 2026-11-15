"""Per-game YAML config loader.

A game config defines:
  - meta (name, process exe, description shown to the agent)
  - optional Windows benchmark scorer observation path + implementation
  - optional recorded `menu_skip` pad sequence (title screen -> in-game)
  - `actions` map: semantic name -> gamepad state

Each action's gamepad state is a dict with optional fields:
  description: str            (required)
  buttons:     list[str]      (default [])  e.g. ['SOUTH'] or ['A'] (alias)
  left_stick:  [float, float] (default [0,0]) each in [-1, 1]
  right_stick: [float, float] (default [0,0])
  left_trigger:  float        (default 0)   in [0, 1]
  right_trigger: float        (default 0)
  duration_seconds: float     (default 0.15)  — recorded startup hold time

Windows raw benchmark actions deliberately omit `duration_seconds`: an
`ACT <tics> ...` command supplies the only gameplay hold time, converted with
the scenario's `runtime.tics_per_second`. Recorded `menu_skip` entries are
wall-clock startup trajectories and use ActionSpec.duration_seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import math
from pathlib import Path
from typing import Any
from typing import Optional

import yaml

from coding4game_gui.benchmark.windows.save_state import SaveStateSpec
from coding4game_gui.benchmark.windows.save_state import parse_save_state
from coding4game_gui.paths import GAMES_DIR
from coding4game_gui.benchmark.windows.gamepad import resolve_button
from coding4game_gui.benchmark.windows.scorers.factory import validate_scorer_selection


@dataclass
class ActionSpec:
    """One named driver action -> one gamepad state (or one mouse click)."""
    name: str
    description: str
    buttons: list[str] = field(default_factory=list)
    left_stick: tuple[float, float] = (0.0, 0.0)
    right_stick: tuple[float, float] = (0.0, 0.0)
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    duration_seconds: float = 0.15
    # Optional MOUSE action: (x, y) in GAME-SPACE pixels — the renderer's
    # output frame, i.e. the same space the scorer's calibrated rects live
    # in. When set, the bench clicks there instead of driving the pad.
    # Mouse-native games (menus, card games) may use this path instead of a
    # focus ring. Requires an interactive session: mouse injection does not
    # cross the session-isolation wall.
    click: Optional[tuple[float, float]] = None
    # Navigation in games that lock the pointer to the reticle: press the
    # mouse button at its current position without attempting a cursor move.
    click_current: bool = False
    # Navigation text entered into the focused search field.
    type_text: Optional[str] = None
    # Optional TEXT-TARGETED mouse action: click whatever on-screen element
    # currently matches this regex, resolved per frame from the OCR the
    # scorer already runs (memoised, so it costs nothing extra). Use it when
    # a control MOVES: Balatro's blind "Select" button sits in the left,
    # middle or right column depending on which blind is offered, so a fixed
    # coordinate works for the first blind of an ante and misses the rest.
    # Keeping one stable token (SELECT_BLIND) rather than three positional
    # ones is what makes this a semantic action layer.
    click_text: Optional[str] = None
    # Navigation only (menu_skip / reset steps): text the DESTINATION screen
    # must show for this step to count as landed. See _navigate — verifying
    # "the screen changed" cannot tell arrival from a transition still in
    # flight, because a transition IS change.
    expect_text: Optional[str] = None
    # Text that must be GONE for the step to count as landed. Some screens
    # are overlays: Balatro's New Run panel leaves the blind screen legible
    # behind it, so anchoring on what is visible cannot distinguish "the run
    # started" from "the panel is still covering it" — and a check that was
    # already satisfied made the closed loop skip its own click.
    expect_absent: Optional[str] = None

    def gamepad_state(self) -> dict:
        """Return the controls accepted by the driver environment."""
        return {
            "buttons": list(self.buttons),
            "left_stick": tuple(self.left_stick),
            "right_stick": tuple(self.right_stick),
            "left_trigger": float(self.left_trigger),
            "right_trigger": float(self.right_trigger),
        }


@dataclass
class PadActionSpec:
    """One entry in a pad-action step list (menu_skip): an ActionSpec plus
    which pad to drive, with an optional xspeedhack speed override.

    `speed` lets a step run at a different game-time multiplier (e.g. menu
    interaction at 1.0x, cutscene fast-forward at 4.0x). When None, the step
    runs at the replayer's default speed.
    """
    pad: int                       # 0 = P1, 1 = P2
    spec: ActionSpec
    speed: Optional[float] = None  # per-step speed; None = use phase default
    # Calibrated startup correction after the first recorded hold misses an
    # explicit text destination. Omission repeats the original hold as before.
    retry_duration_seconds: Optional[float] = None
    # A measured whole-frame brightness floor for guarded neutral waits.
    min_scene_luma: Optional[float] = None


@dataclass
class AutoLaunchSpec:
    """How a one-command launcher should bring the game window up.

    Only launches + waits for the window; all menu navigation lives in the
    game's recorded `menu_skip`. Launches are direct executable starts: the
    harness never invokes Steam or another URI handler.

    `launch_args` covers titles that refuse to boot from their own executable
    and must be started through a platform client's CLI instead (see
    game_launch's module docstring for the measured Forza Horizon 5 case).
    The target is still an absolute local .exe, so the no-URI rule is intact;
    note that when it points at a client, `process:` names the GAME while the
    launched exe is the client, so the window wait must key on `process:`.
    """
    launch_path: Optional[str] = None     # absolute local .exe path
    window_wait_seconds: float = 120.0    # how long to wait for the window
    settle_seconds: float = 60.0          # fixed wait before menu_skip replay
    # Arguments appended to launch_path. Empty for every game that boots from
    # its own exe, which is all of them except the GDK-hybrid case above.
    launch_args: list[str] = field(default_factory=list)
    # The desktop shell can initiate GUI games from a long-lived host runner.
    launch_method: str = "process"


@dataclass
class GameConfig:
    name: str
    process: str
    description: str
    actions: dict[str, ActionSpec]
    source_path: Optional[Path] = None
    screenshot_backend: Optional[str] = None  # Named capture backend, or None for auto-detection.
    # Default scorer observation path and concrete implementation for Windows
    # benchmark scenarios. A scenario may override them, while calibrated
    # regions/thresholds stay in benchmark/scenarios/<scenario>.yaml.
    scorer_kind: Optional[str] = None
    scorer_implementation: Optional[str] = None
    # Optional platform pin. When set, the runner must select this platform
    # (via --platform) or it will refuse to start. None means the game is
    # platform-neutral — the runner uses --platform (default auto).
    # Known values come from coding4game_gui.benchmark.windows.drivers.registry.list_platforms().
    platform: Optional[str] = None
    # Opt in to xspeedhack for infrastructure-controlled zero-timescale input
    # settling.  Most games retain the historical no-speedhack default.
    use_speedhack: bool = False
    # Only an explicitly opted-in HWND loses its text input context at startup.
    ime_context_mode: str = 'system'
    save_state: Optional[SaveStateSpec] = None
    # The ONE pre-agent sequence: a recorded ordered timeline (both pads,
    # title → in-game) replayed verbatim before the agent takes over.
    menu_skip: list[PadActionSpec] = field(default_factory=list)
    # Optional auto-launch spec: only launches the game window (menu nav lives
    # in menu_skip).
    auto_launch: Optional[AutoLaunchSpec] = None


# ---------- parsing ----------

def _coerce_pair(value: Any, default: tuple[float, float]) -> tuple[float, float]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    raise ValueError(f"expected [x, y] pair, got {value!r}")


def _coerce_buttons(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"buttons must be a list, got {value!r}")
    out = []
    for raw in value:
        token = str(raw).upper()
        if token.startswith("KEY[") and token.endswith("]"):
            from coding4game_gui.benchmark.windows.drivers.windows_kbm.input import SCANCODES

            if token[4:-1] not in SCANCODES:
                raise ValueError(f"unsupported recorded keyboard key: {raw!r}")
            out.append(token)
            continue
        resolved = resolve_button(str(raw))
        if resolved is None:
            raise ValueError(
                f"unknown button '{raw}' (allowed: A/B/X/Y, SOUTH/EAST/WEST/NORTH, "
                f"DPAD_*, LEFT_SHOULDER/RIGHT_SHOULDER, LEFT_TRIGGER/RIGHT_TRIGGER, "
                f"LEFT_THUMB/RIGHT_THUMB, START/BACK/GUIDE — see benchmark/windows/gamepad.py)"
            )
        out.append(resolved)
    return out


def _parse_pad_action_entry(entry: dict[str, Any], name: str, ctx: str,
                            default_pad: int = 1) -> "PadActionSpec":
    """Parse one step entry (used by menu_skip) into a PadActionSpec.
    Keep navigation-only readiness and retry settings outside gameplay actions.
    """
    if 'keys' in entry:
        raise ValueError(f'{ctx}: unsupported keys field; migrate recorded keys to buttons: [KEY[...]]')
    pad_raw = entry.get("pad", default_pad)
    try:
        pad_yaml = int(pad_raw)
    except (TypeError, ValueError):
        raise ValueError(f"{ctx}.pad must be 1 or 2, got {pad_raw!r}")
    if pad_yaml not in (1, 2):
        raise ValueError(f"{ctx}.pad must be 1 or 2, got {pad_yaml}")
    speed_raw = entry.get("speed")
    speed: Optional[float] = None
    if speed_raw is not None:
        try:
            speed = float(speed_raw)
        except (TypeError, ValueError):
            raise ValueError(f"{ctx}.speed must be a number, got {speed_raw!r}")
    action_dict = {k: v for k, v in entry.items()
                   if k not in ("pad", "speed", "retry_duration_seconds", "min_scene_luma")}
    spec = _parse_action(name, action_dict)
    retry = entry.get("retry_duration_seconds")
    if retry is not None:
        has_input = (spec.buttons or any(spec.left_stick) or any(spec.right_stick)
                     or spec.left_trigger or spec.right_trigger)
        if (isinstance(retry, bool) or not isinstance(retry, (int, float))
                or not math.isfinite(retry) or not 0 < retry <= spec.duration_seconds
                or not (spec.expect_text or spec.expect_absent) or not has_input
                or spec.click is not None or spec.click_current or spec.type_text):
            raise ValueError(
                f"{ctx}.retry_duration_seconds requires guarded pad/key input "
                "and a finite positive duration no longer than the recorded hold")
        retry = float(retry)
    luma = entry.get("min_scene_luma")
    if luma is not None:
        has_input = (bool(spec.buttons) or any(spec.left_stick) or any(spec.right_stick)
                     or spec.left_trigger or spec.right_trigger or spec.click is not None
                     or spec.click_current or spec.type_text)
        if (isinstance(luma, bool) or not isinstance(luma, (int, float))
                or not math.isfinite(luma) or not 0 < luma <= 255
                or has_input or not (spec.expect_text or spec.expect_absent)):
            raise ValueError(f"{ctx}.min_scene_luma requires a guarded neutral wait and a finite value in (0,255]")
        luma = float(luma)
    return PadActionSpec(pad=pad_yaml - 1, spec=spec, speed=speed,
                         retry_duration_seconds=retry, min_scene_luma=luma)


def _parse_action(name: str, raw: dict[str, Any]) -> ActionSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"action '{name}': expected dict, got {type(raw).__name__}")
    description = str(raw.get("description") or name).strip()
    if not isinstance(raw.get("click_current", False), bool):
        raise ValueError(f"action '{name}': click_current must be boolean")
    if raw.get("click_current") and raw.get("click") is not None:
        raise ValueError(f"action '{name}': click and click_current are exclusive")
    if raw.get("type_text") is not None and (
            not isinstance(raw["type_text"], str) or not raw["type_text"]):
        raise ValueError(f"action '{name}': type_text must be nonempty text")
    if raw.get("type_text") is not None and (
            raw.get("click") is not None or raw.get("click_current")):
        raise ValueError(f"action '{name}': type_text cannot share a mouse step")
    return ActionSpec(
        name=name,
        description=description,
        buttons=_coerce_buttons(raw.get("buttons")),
        left_stick=_coerce_pair(raw.get("left_stick"), (0.0, 0.0)),
        right_stick=_coerce_pair(raw.get("right_stick"), (0.0, 0.0)),
        left_trigger=float(raw.get("left_trigger", 0.0) or 0.0),
        right_trigger=float(raw.get("right_trigger", 0.0) or 0.0),
        duration_seconds=float(raw.get("duration_seconds", 0.15) or 0.15),
        click_text=(str(raw["click_text"]).strip()
                    if raw.get("click_text") else None),
        expect_text=(str(raw["expect"]).strip()
                     if raw.get("expect") else None),
        expect_absent=(str(raw["expect_absent"]).strip()
                       if raw.get("expect_absent") else None),
        click=(_coerce_pair(raw["click"], (0.0, 0.0))
               if raw.get("click") is not None else None),
        click_current=bool(raw.get("click_current", False)),
        type_text=raw.get("type_text"),
    )


def parse_game_config(data: dict[str, Any], source_path: Optional[Path] = None,
                      *, validate_files: bool = True) -> GameConfig:
    if not isinstance(data, dict):
        raise ValueError("config must be a YAML mapping at the top level")
    name = str(data.get("name") or (source_path.stem if source_path else "?")).strip()
    process = str(data.get("process") or "").strip()
    if not process:
        raise ValueError("config: 'process' (exe name) is required")
    description = str(data.get("description") or "").strip()
    ime_context_mode = data.get('ime_context_mode', 'system')
    if ime_context_mode not in ('system', 'disabled'):
        raise ValueError("config: ime_context_mode must be 'system' or 'disabled'")

    actions_raw = data.get("actions") or {}
    if not isinstance(actions_raw, dict) or not actions_raw:
        raise ValueError("config: 'actions' must be a non-empty mapping")
    actions: dict[str, ActionSpec] = {}
    for k, v in actions_raw.items():
        action_name = str(k).strip()
        if not action_name.replace("_", "").isalnum():
            raise ValueError(f"action name '{action_name}' must be alphanumeric / underscores")
        actions[action_name] = _parse_action(action_name, v)

    raw_backend = data.get("screenshot_backend")
    screenshot_backend: Optional[str] = None
    if raw_backend is not None:
        screenshot_backend = str(raw_backend).strip().lower()
        if screenshot_backend not in ("printwindow", "printwindow_visible", "visible_client", "dxcam", "pyautogui"):
            raise ValueError(
                f"config: 'screenshot_backend' must be one of "
                f"'printwindow', 'printwindow_visible', 'visible_client', 'dxcam', 'pyautogui'; got {raw_backend!r}"
            )

    raw_platform = data.get("platform")
    platform_pin: Optional[str] = None
    if raw_platform is not None:
        platform_pin = str(raw_platform).strip().lower() or None
        # No registry validation here — that would force an import of
        # `drivers.registry` (which imports vgamepad on Windows) just to
        # load a YAML. The runner validates against the live registry.

    use_speedhack_raw = data.get("use_speedhack", False)
    if not isinstance(use_speedhack_raw, bool):
        raise ValueError(
            "config: 'use_speedhack' must be a boolean; "
            f"got {use_speedhack_raw!r}"
        )
    use_speedhack = use_speedhack_raw

    scorer_raw = data.get("scorer")
    scorer_kind: Optional[str] = None
    scorer_implementation: Optional[str] = None
    if scorer_raw is not None:
        if not isinstance(scorer_raw, dict):
            raise ValueError("config: 'scorer' must be a mapping")
        scorer_kind = str(scorer_raw.get("kind") or "").strip().lower()
        if not scorer_kind:
            raise ValueError("config: 'scorer.kind' is required when scorer is declared")
        scorer_implementation = str(
            scorer_raw.get("implementation") or "").strip().lower()
        if not scorer_implementation:
            raise ValueError(
                "config: 'scorer.implementation' is required when scorer is declared")
        validate_scorer_selection(scorer_kind, scorer_implementation)

    save_state_raw = data.get("save_state")
    save_state_spec = (parse_save_state(save_state_raw, validate_files=validate_files)
                       if save_state_raw is not None else None)

    auto_launch_raw = data.get("auto_launch")
    auto_launch_spec: Optional[AutoLaunchSpec] = None
    if auto_launch_raw is not None:
        if not isinstance(auto_launch_raw, dict):
            raise ValueError("config: 'auto_launch' must be a mapping")
        if auto_launch_raw.get("steam_url"):
            raise ValueError(
                "config: auto_launch.steam_url is disabled; use an absolute "
                "auto_launch.launch_path to start the game executable directly"
            )
        launch_args_raw = auto_launch_raw.get("launch_args") or []
        if isinstance(launch_args_raw, (str, bytes)):
            raise ValueError(
                "config: auto_launch.launch_args must be a list of strings, "
                "not a single string")
        if not isinstance(launch_args_raw, list):
            raise ValueError(
                "config: auto_launch.launch_args must be a list of strings")
        launch_args: list[str] = []
        for i, value in enumerate(launch_args_raw):
            if not isinstance(value, str):
                raise ValueError(
                    f"config: auto_launch.launch_args[{i}] must be a string, "
                    f"got {value!r}")
            launch_args.append(value)
        launch_method = auto_launch_raw.get("launch_method", "process")
        if launch_method not in ("process", "desktop_shell"):
            raise ValueError(
                "config: auto_launch.launch_method must be 'process' or 'desktop_shell'")
        auto_launch_spec = AutoLaunchSpec(
            launch_path=(str(auto_launch_raw["launch_path"]).strip()
                         if auto_launch_raw.get("launch_path") else None),
            window_wait_seconds=float(auto_launch_raw.get("window_wait_seconds", 120.0)),
            settle_seconds=float(auto_launch_raw.get("settle_seconds", 60.0)),
            launch_args=launch_args,
            launch_method=launch_method,
        )

    # ---- the one pre-agent sequence: menu_skip ----
    # A single ordered list of pad-action steps (both pads, title → in-game).
    # Each step carries `pad:` (1=P1, 2=P2 → 0-based) and an optional `speed:`.
    menu_skip_raw = data.get("menu_skip") or []
    if not isinstance(menu_skip_raw, list):
        raise ValueError("config: 'menu_skip' must be a list")
    menu_skip: list[PadActionSpec] = []
    for i, entry in enumerate(menu_skip_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"menu_skip[{i}]: must be a dict")
        menu_skip.append(_parse_pad_action_entry(
            entry, f"_menu_skip_{i}", f"menu_skip[{i}]", default_pad=1,
        ))

    return GameConfig(
        name=name, process=process, description=description,
        actions=actions, source_path=source_path,
        screenshot_backend=screenshot_backend,
        platform=platform_pin,
        use_speedhack=use_speedhack,
        ime_context_mode=ime_context_mode,
        scorer_kind=scorer_kind,
        scorer_implementation=scorer_implementation,
        save_state=save_state_spec,
        menu_skip=menu_skip,
        auto_launch=auto_launch_spec,
    )


# ---------- public loader ----------

def resolve_game_path(name_or_path: str) -> Path:
    """Resolve --game arg into an absolute YAML path."""
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p.resolve()
    candidate = (GAMES_DIR / f"{name_or_path}.yaml").resolve()
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"game config not found for '{name_or_path}'. "
        f"Looked at {p} and {candidate}. Available: {list_games()}"
    )


def load_game_config(name_or_path: str, *, validate_files: bool = True) -> GameConfig:
    """Load a driver; metadata callers may defer machine-local file checks."""
    path = resolve_game_path(name_or_path)
    from coding4game_gui.benchmark.windows.local_paths import read_driver_data
    data = read_driver_data(path)
    return parse_game_config(data, source_path=path, validate_files=validate_files)


def list_games() -> list[str]:
    """List the bare names of every game config in the games/ dir."""
    if not GAMES_DIR.exists():
        return []
    return sorted(p.stem for p in GAMES_DIR.glob("*.yaml") if not p.stem.startswith("_"))
