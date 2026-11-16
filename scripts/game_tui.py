#!/usr/bin/env python
"""Operator menu for host setup, development, independent tests and recording."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------- terminal setup
if os.name == "nt":
    os.system("")  # turn on ANSI escape processing on modern Windows consoles
    try:
        import ctypes

        _k = ctypes.windll.kernel32
        _k.SetConsoleMode(_k.GetStdHandle(-11), 7)
    except Exception:
        pass
try:
    sys.stdout.reconfigure(encoding="utf-8")  # box chars + glyphs render cleanly
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
LAUNCHERS = SCRIPTS / "launchers"
SCORER_SESSION_RECORDER = (
    REPO / ".agents" / "skills" / "add-new-game" / "scripts"
    / "record_scorer_session.py"
)
sys.path.insert(0, str(REPO))

if __name__ == "__main__" and (
    not (3, 11) <= sys.version_info[:2] < (3, 14) or sys.maxsize <= 2**32
):
    sys.exit(
        f"Coding4Games requires 64-bit Python 3.11 to 3.13. Selected Python {sys.version.split()[0]}.\n"
        "Use ./scripts/start.ps1 -Python with a prepared 64-bit Python environment."
    )

try:
    import yaml
    from coding4game_gui.benchmark.windows.games_config import load_game_config
except ImportError as error:
    if error.name and error.name.startswith("coding4game_gui"):
        raise
    dependency = "PyYAML" if error.name == "yaml" else error.name or "A required package"
    sys.exit(
        f"{dependency} could not be imported from the selected Python environment.\n"
        "Run ./scripts/start.ps1 -Setup to prepare the project environment, "
        "or use -Python with a prepared interpreter."
    )

from coding4game_gui.benchmark.experiment_plan import BENCHMARK_MODULE
from coding4game_gui.benchmark.experiment_plan import BenchmarkScenario
from coding4game_gui.benchmark.experiment_plan import DevelopmentPlan
from coding4game_gui.benchmark.experiment_plan import ProgramTestPlan
from coding4game_gui.benchmark.experiment_plan import build_development_args
from coding4game_gui.benchmark.experiment_plan import build_program_test_args
from coding4game_gui.benchmark.experiment_plan import discover_benchmark_scenarios
from coding4game_gui.benchmark.windows.local_paths import read_driver_data
from coding4game_gui.paths import GAMES_DIR
from coding4game_gui.operator_config import (
    load_preset, resolve_path, save_preset, validate_preset,
)

LAUNCH_PRESET = REPO / "launcher.local.json"

# ---------------------------------------------------------------- palette / ansi
R = "\x1b[0m"
B = "\x1b[1m"
DIM = "\x1b[2m"
ITAL = "\x1b[3m"
ACCENT = "\x1b[38;2;215;119;87m"      # coral (Claude-ish)
ACCENT_B = "\x1b[1m\x1b[38;2;226;134;102m"
GREEN = "\x1b[38;2;126;200;130m"
RED = "\x1b[38;2;233;112;100m"
CYAN = "\x1b[38;2;120;190;220m"
PURPLE = "\x1b[38;2;176;142;230m"
YELLOW = "\x1b[38;2;226;192;108m"
GRAY = "\x1b[38;2;138;138;142m"
SOFT = "\x1b[38;2;200;200;205m"
SEL = "\x1b[48;2;215;119;87m\x1b[1m\x1b[38;2;26;24;23m"   # coral bar, ink text
CHIP = "\x1b[48;2;48;46;52m"                              # dark pill background

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def vlen(s: str) -> int:
    """Visible width: strip ANSI, count CJK / wide glyphs as 2 cols."""
    s = _ANSI.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - vlen(s))


def _clip(text: str, width: int) -> str:
    """Fit plain text on one terminal line, leaving room for an ellipsis."""
    text = " ".join(text.splitlines())
    if width <= 0:
        return ""
    if vlen(text) > width:
        used = 0
        for index, char in enumerate(text):
            used += vlen(char)
            if used >= width:
                return text[:index] + "…"
    return text


def pill(text: str, fg: str) -> str:
    return f"{CHIP}{fg} {text} {R}"


def _supports_keys() -> bool:
    return os.name == "nt" and sys.stdin.isatty()


def clear() -> None:
    sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
    sys.stdout.flush()


def hide_cursor() -> None:
    sys.stdout.write("\x1b[?25l")


def show_cursor() -> None:
    sys.stdout.write("\x1b[?25h")


_W = 52  # inner width of the header box


def _box(lines: list[str], color: str = ACCENT) -> str:
    top = f"  {color}╭{'─' * _W}╮{R}"
    bot = f"  {color}╰{'─' * _W}╯{R}"
    body = [f"  {color}│{R}{pad(ln, _W)}{color}│{R}" for ln in lines]
    return "\n".join([top, *body, bot])


def banner(subtitle: str = "", columns: int | None = None) -> int:
    if columns is not None and columns < _W + 4:
        print(f"  {ACCENT_B}{_clip('CODING4GAME · Game Launcher', columns - 2)}{R}")
        rows = 1
    else:
        logo = f" {ACCENT_B}🎮  CODING4GAME{R}  {GRAY}·{R}  {SOFT}Game Launcher{R}"
        spark = f"   {ACCENT}◆{R} {YELLOW}◆{R} {GREEN}◆{R} {CYAN}◆{R} {PURPLE}◆{R}"
        print(_box([logo, spark]))
        rows = 4
    if subtitle:
        if columns is not None:
            subtitle = _clip(subtitle, columns - 2)
        print(f"  {GRAY}{subtitle}{R}")
        rows += 1
    print()
    return rows + 1


# ---------------------------------------------------------------- key reader
def read_key() -> str:
    """Return one of: up, down, enter, esc, char, or the raw character."""
    if not _supports_keys():
        return "input"
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, "other")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("k", "K"):
        return "up"
    if ch in ("j", "J"):
        return "down"
    if ch in ("q", "Q"):
        return "esc"
    return ch


# ---------------------------------------------------------------- menu component
_KEYS = (f"{pill('↑↓', SOFT)} {GRAY}move{R}   {pill('⏎', SOFT)} {GRAY}select{R}   "
         f"{pill('esc', SOFT)} {GRAY}back{R}")


def _seg(it: dict) -> str:
    """Plain 'icon label' segment (no colour), for width measuring + padding."""
    icon = it.get("icon", "•")
    return f"{icon}  {it['label']}"


def menu(title: str, items: list[dict], subtitle: str = "",
         footer: str | None = None,
         start_index: int | None = None) -> int | None:
    """
    items: list of {label, hint?, icon?, tag?, tag_color?, disabled?}.
    Returns the selected index, or None on Esc.
    Falls back to numbered input when arrow keys aren't available.
    """
    if not items:
        return None
    selectable = [i for i, it in enumerate(items) if not it.get("disabled")]
    segs = [_seg(it) for it in items]

    def chip(it: dict) -> str:
        return f"  {pill(it['tag'], it.get('tag_color', GRAY))}" if it.get("tag") else ""

    if not selectable or not _supports_keys():
        clear()
        banner(subtitle)
        print(f"  {B}{title}{R}\n")
        for i, it in enumerate(items, 1):
            dis = it.get("disabled")
            num = f"{GRAY} -{R}" if dis else f"{ACCENT}{i:>2}{R}"
            seg = f"{GRAY}{segs[i-1]}{R}" if dis else f"{SOFT}{segs[i-1]}{R}"
            hint = f"   {GRAY}{it.get('hint','')}{R}" if it.get("hint") else ""
            print(f"  {num}  {seg}{chip(it)}{hint}")
        if not selectable:
            print(f"\n  {YELLOW}No options are available.{R}")
            pause()
            return None
        print(f"\n  {GRAY}type a number, Enter to cancel{R}")
        while True:
            raw = input(f"\n  {ACCENT}❯{R} ").strip()
            if not raw:
                return None
            try:
                idx = int(raw) - 1
            except ValueError:
                idx = None
            if idx in selectable:
                return idx
            print(f"  {YELLOW}Choose an available number or press Enter to cancel.{R}")

    pos = start_index if (start_index in selectable) else selectable[0]
    widest_label = max(map(vlen, segs))
    first_visible = 0
    hide_cursor()
    try:
        while True:
            clear()
            columns, rows = shutil.get_terminal_size((80, 24))
            columns -= 1
            header_rows = banner(subtitle, columns)
            # Leave room for the title, key guide, and a final blank line.
            page_size = min(len(items), max(1, rows - header_rows - 5))
            first_visible = min(first_visible, len(items) - page_size)
            if pos < first_visible:
                first_visible = pos
            elif pos >= first_visible + page_size:
                first_visible = pos - page_size + 1
            end = first_visible + page_size
            page = f"  {first_visible + 1}–{end} of {len(items)}" if page_size < len(items) else ""
            heading = _clip(title, columns - 2 - vlen(page))
            print(f"  {B}{heading}{R}{GRAY}{page}{R}\n")

            label_width = min(widest_label, max(1, (columns - 6) * 2 // 3))
            space_after_label = columns - 6 - label_width
            for i in range(first_visible, end):
                item = items[i]
                label = pad(_clip(segs[i], label_width), label_width)
                tag = chip(item)
                if vlen(tag) > space_after_label:
                    tag = ""
                hint = _clip(item.get("hint") or "", space_after_label - vlen(tag) - 2)
                if i == pos:
                    lead = f"  {SEL} ❯ {label} {R}"
                    tone = ACCENT
                else:
                    tone = DIM if item.get("disabled") else GRAY
                    lead = f"      {DIM if item.get('disabled') else SOFT}{label}{R}"
                detail = f"  {tone}{hint}{R}" if hint else ""
                print(f"{lead}{tag}{detail}")
            keys = footer or _KEYS
            if vlen(keys) > columns - 2:
                keys = _clip(_ANSI.sub("", keys), columns - 2)
            print(f"\n  {keys}")

            key = read_key()
            if key == "up":
                cur = selectable.index(pos)
                pos = selectable[(cur - 1) % len(selectable)]
            elif key == "down":
                cur = selectable.index(pos)
                pos = selectable[(cur + 1) % len(selectable)]
            elif key == "enter":
                return pos
            elif key == "esc":
                return None
    finally:
        show_cursor()


# ---------------------------------------------------------------- input helpers
BACK = object()  # sentinel: user asked to step back


def field(prompt: str, default: str = "", help_lines: list[str] | None = None,
          allow_back: bool = False):
    if help_lines:
        print(f"  {DIM}┌ what this means{R}")
        for ln in help_lines:
            print(f"  {DIM}│{R} {GRAY}{ln}{R}")
        print(f"  {DIM}└{R}")
    suffix = f" {GRAY}({default}){R}" if default else ""
    show_cursor()
    val = input(f"  {ACCENT}❯{R} {B}{prompt}{R}{suffix}: ").strip()
    if allow_back and val in ("<", "back"):
        return BACK
    return val or default


def confirm(prompt: str, default: bool = True) -> bool:
    d = f"{GRAY}[Y/n]{R}" if default else f"{GRAY}[y/N]{R}"
    show_cursor()
    raw = input(f"  {ACCENT}❯{R} {prompt} {d} ").strip().lower()
    return default if not raw else raw in ("y", "yes")


def int_field(
    prompt: str,
    default: int,
    minimum: int = 1,
    help_lines: list[str] | None = None,
):
    """Read an integer, returning BACK when the user types ``<``."""
    while True:
        raw = field(
            prompt, str(default), help_lines=help_lines, allow_back=True)
        if raw is BACK:
            return BACK
        try:
            value = int(raw)
        except ValueError:
            print(f"  {RED}enter a whole number (or < to go back){R}")
            continue
        if value < minimum:
            print(f"  {RED}must be at least {minimum}{R}")
            continue
        return value


def pause() -> None:
    show_cursor()
    input(f"\n  {GRAY}press Enter to continue{R} ")


# ---------------------------------------------------------------- discovery / validate
def game_files() -> list[Path]:
    drivers = {scenario.driver_game for scenario in discover_benchmark_scenarios()
               if scenario.driver_game}
    return [path for driver in sorted(drivers)
            if (path := GAMES_DIR / f"{driver}.yaml").is_file()]


def validate(path: Path) -> tuple[bool, str]:
    try:
        load_game_config(str(path))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as error:
        return False, str(error)
    return True, "valid"


def _load_yaml(slug: str) -> dict:
    try:
        data = read_driver_data(GAMES_DIR / f"{slug}.yaml")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _proc_running(exe: str) -> bool:
    if not exe:
        return False
    try:
        import psutil
    except Exception:
        return False
    exe_l = exe.lower()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == exe_l:
                return True
        except Exception:
            continue
    return False


def kill_game(slug: str) -> None:
    """Terminate the game process(es) for `slug` (graceful, then force)."""
    proc = str(_load_yaml(slug).get("process") or "").strip()
    if not proc:
        return
    try:
        import psutil
    except Exception:
        print(f"  {RED}psutil unavailable; close {proc} manually.{R}")
        return
    targets = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == proc.lower():
                targets.append(p)
        except Exception:
            continue
    if not targets:
        return
    for p in targets:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(targets, timeout=4)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    print(f"  {GREEN}✓{R} closed {proc} ({len(targets)} process(es))")


def close_or_pause(slug: str) -> None:
    """After a launched game returns/was interrupted: Enter closes the game."""
    proc = str(_load_yaml(slug).get("process") or "").strip()
    if not (proc and _proc_running(proc)):
        pause()
        return
    show_cursor()
    prompt = (f"\n  {SOFT}{proc} is still running.{R}  "
              f"{GRAY}Enter = close it · type{R} {SOFT}k{R} {GRAY}= keep it open{R} ")
    try:
        ans = input(prompt).strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = ""  # Ctrl-C at the prompt -> close, as the user asked
    if ans != "k":
        kill_game(slug)


# ---------------------------------------------------------------- command execution
def command_text(cmd: list[str]) -> str:
    """Render a Windows-safe command preview without executing it."""
    return subprocess.list2cmdline(cmd)


def run_command(cmd: list[str], *, env=None) -> int:
    clear()
    banner()
    print(f"  {GRAY}${R} {CYAN}{command_text(cmd)}{R}\n")
    print(f"  {GRAY}{'─' * 50}{R}\n")
    show_cursor()
    try:
        status = subprocess.run(cmd, cwd=REPO, env=env, check=False).returncode
    except KeyboardInterrupt:
        print(f"\n  {RED}interrupted{R}")
        status = 130
    except OSError as error:
        print(f"\n  {RED}Could not start the command: {error}{R}")
        status = 1
    print(f"\n  {GRAY}{'─' * 50}{R}")
    return status


def preview_and_run_launcher(script: str, args: list[str], question: str) -> None:
    """Preview a shared launcher before offering execution."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        print("PowerShell is required for this operation.")
        pause()
        return
    command = [shell, "-NoProfile", "-File", str(LAUNCHERS / script),
               "-Python", sys.executable, *args]
    if run_command(command) == 0 and confirm(question, default=False):
        run_command([*command, "-Execute"])
    pause()


def run_python_script(script_path: Path, args: list[str]) -> None:
    run_command([sys.executable, str(script_path), *args])


def run_python_module(module: str, args: list[str], *, env=None) -> None:
    run_command([sys.executable, "-m", module, *args], **({"env": env} if env is not None else {}))


def run_script(script: str, args: list[str]) -> None:
    run_python_script(SCRIPTS / script, args)


def record_startup(scenario: BenchmarkScenario) -> None:
    """Cold-start the registered scenario and record its menu navigation."""
    slug = scenario.driver_game
    clear()
    banner("Record menu navigation")
    print(f"  {B}🎬 Record menu-skip · {slug}{R}\n")
    print("  The recorder cold-starts the scenario. Use the virtual pad to reach the task.")
    print("  Pauses between inputs become recorded waits.")
    if _load_yaml(slug).get("menu_skip"):
        print(f"  {YELLOW}Saving replaces the current menu_skip sequence.{R}")
    print("  Choose SAVE to write the sequence or CANCEL to discard it.")
    if confirm("Start recording now?", default=True):
        run_script("calibration/record_pad_to_yaml.py",
                   ["--game", slug, "--scenario", scenario.slug, "--auto-launch"])
        close_or_pause(slug)


def record_scorer_calibration(slug: str) -> None:
    """Cold-start, replay menu_skip, then record a human playthrough."""
    clear()
    banner("auto-launch -> menu-skip -> scorer evidence")
    print(f"  {B}Record scorer calibration - {slug}{R}\n")
    print(f"  {GRAY}The game will be cold-started and menu_skip replayed, then")
    print("  handed to you for manual play. Create the printed stop-request")
    print(f"  file to finish recording and leave the game open.{R}\n")
    if not confirm("Start scorer calibration now?", default=True):
        return
    run_python_script(
        SCORER_SESSION_RECORDER,
        ["--scenario", slug, "--fps", "10", "--until-stopped"],
    )
    pause()


# ---------------------------------------------------------------- experiment launcher
def _scenario_ready(scenario: BenchmarkScenario) -> tuple[bool, str]:
    if scenario.engine != "windows":
        return True, "ready"
    if not scenario.driver_game:
        return False, "missing driver_game"
    driver_path = GAMES_DIR / f"{scenario.driver_game}.yaml"
    if not driver_path.is_file():
        return False, f"missing games/{scenario.driver_game}.yaml"
    return validate(driver_path)


def select_benchmark_scenario(
    title: str,
    *,
    engine: str | None = None,
    driver_game: str | None = None,
) -> BenchmarkScenario | None:
    scenarios = [
        scenario for scenario in discover_benchmark_scenarios()
        if (engine is None or scenario.engine == engine)
        and (driver_game is None or scenario.driver_game == driver_game)
    ]
    items = []
    for scenario in scenarios:
        ready, reason = _scenario_ready(scenario)
        items.append({
            "label": scenario.slug,
            "icon": "🪟" if scenario.engine == "windows" else "👾",
            "tag": scenario.engine,
            "tag_color": CYAN if scenario.engine == "windows" else PURPLE,
            "hint": scenario.title if ready else reason,
            "disabled": not ready,
            "_scenario": scenario,
        })
    if not items:
        clear(); banner(); print(f"  {RED}No benchmark scenarios found.{R}"); pause()
        return None
    selected = menu(title, items, subtitle="registered benchmark scenarios only")
    return None if selected is None else items[selected]["_scenario"]


def launch_direct_baseline(scenario: BenchmarkScenario) -> None:
    clear(); banner(f"Direct baseline · {scenario.slug}")
    try:
        default_model = load_preset(LAUNCH_PRESET)["model"]
    except ValueError as error:
        print(error); pause(); return
    model = field("LiteLLM model", default_model, allow_back=True)
    if model is BACK or not model.strip():
        return
    episodes = int_field("Independent episodes", 3)
    if episodes is BACK:
        return
    tics = int_field("Maximum game tics per episode", 10000)
    if tics is BACK:
        return
    preview_and_run_launcher("run_baseline.ps1", [
        "-Games", scenario.slug, "-Models", model.strip(), "-Episodes", str(episodes),
        "-TicBudget", str(tics),
    ], "Launch this real-token Direct baseline?")


def _current_program_fields(scenario):
    profile = field("WSL2 profile.json", "", allow_back=True)
    if profile is BACK or not profile.strip():
        return None
    mode = menu("Supplied stepped input", [
        {"label": "Gamepad", "icon": ">", "hint": "physical gamepad controls"},
        {"label": "Keyboard/mouse", "icon": ">", "hint": "physical keyboard/mouse controls"},
    ])
    if mode is None:
        return None
    tics = int_field("Game tic budget", 1200)
    if tics is BACK:
        return None
    wall = int_field("Wall deadline in seconds", 300)
    if wall is BACK:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return dict(scenario=scenario, profile=Path(profile), tic_budget=tics,
                wall_seconds=wall, windows_input=("gamepad","gamepad-kbm")[mode],
                out_dir=REPO/"out"/"windows_bench"/f"{stamp}_{scenario.slug}")


def launch_manual_submission_test() -> None:
    scenario = select_benchmark_scenario("Frozen program test", engine="windows")
    if scenario is None:
        return
    fields = _current_program_fields(scenario)
    if fields is None:
        return
    source = field("Source programs directory", "", allow_back=True)
    if source is BACK or not source.strip():
        return
    program_id = field("Program ID (blank uses explicitly selected final)", "", allow_back=True)
    if program_id is BACK:
        return
    fields["out_dir"] = fields["out_dir"].with_name(fields["out_dir"].name+"_test")
    plan = ProgramTestPlan(**fields, source_programs=Path(source), program_id=program_id or None)
    try:
        args = build_program_test_args(plan, execute=True)
    except ValueError as exc:
        print(f"Invalid test: {exc}"); pause(); return
    print(command_text([sys.executable,"-m",BENCHMARK_MODULE,*args]))
    print(f"No model calls. Wall limit {plan.wall_seconds}s; game budget {plan.tic_budget} tics.")
    print("Creates a clean WSL2 final-test environment, restores registered game start, launches/closes game; writes new evidence.")
    if confirm("Run this independent real-game test?", default=False):
        run_python_module(BENCHMARK_MODULE,args)


def final_tests_menu() -> None:
    selected = menu("Independent final tests", [
        {"label": "Test a development run", "hint": "use its selected program and saved settings"},
        {"label": "Top up incomplete tests", "hint": "find completed developments and fill missing valid repeats"},
        {"label": "Advanced: test an archived program"},
    ])
    if selected is None:
        return
    if selected == 2:
        launch_manual_submission_test()
        return
    if selected == 0:
        source = field("Development run directory", "", allow_back=True)
        if source is BACK or not source:
            return
        script, args = "test_code.ps1", ["-RunDirs", source]
    else:
        root = field("Experiment output directory (blank uses launcher settings)", "", allow_back=True)
        if root is BACK:
            return
        script, args = "test_code_remaining.ps1", ["-Root", root] if root else []
    repeats = int_field("Independent test repeats", 3)
    if repeats is not BACK:
        preview_and_run_launcher(
            script, [*args, "-Repeats", str(repeats)],
            "Run the previewed real-game tests without model calls?",
        )


def host_setup_menu() -> None:
    selected = menu("Host setup and diagnostics", [
        {"label": "Prepare Windows Python environment", "hint": "install dependencies and create missing local settings"},
        {"label": "Check host prerequisites", "hint": "read-only doctor checks"},
    ])
    if selected == 0:
        preview_and_run_launcher("setup.ps1", [], "Install the previewed Python dependencies?")
    elif selected == 1:
        run_python_module("coding4game_gui.doctor", [])
        pause()


def launch_experiment() -> None:
    scenario = select_benchmark_scenario("Run Windows experiment", engine="windows")
    if scenario is None:
        return
    selected = menu("Experiment", [
        {"label": "WSL2 Claude Code program development", "icon": ">",
         "hint": "clean Linux environment; evaluated Python submission"},
        {"label": "Direct baseline", "icon": ">",
         "hint": "configured model plays through the registered stepped controls"},
        {"label": "WSL2 Codex program development", "icon": ">",
         "hint": "Linux Codex CLI; scoped host gateway; Python submission"},
    ], subtitle=scenario.title)
    if selected is None:
        return
    if selected == 1:
        launch_direct_baseline(scenario)
    else:
        launch_quick_development(scenario, agent="codex" if selected == 2 else "claude")


def _edit_launch_preset(values):
    updated = dict(values)
    for name, label in (("profile", "WSL2 profile.json"), ("model", "LiteLLM model selector"),
                        ("credentials", "Host LiteLLM config path (never enter a key)")):
        answer = field(label, str(updated[name]), allow_back=True)
        if answer is BACK:
            return None
        updated[name] = answer
    input_modes = ("auto", "gamepad-kbm", "gamepad")
    choice = menu("Supplied stepped input", [
        {"label": "Use scenario default"}, {"label": "Keyboard/mouse"}, {"label": "Gamepad"},
    ], start_index=input_modes.index(updated["windows_input"]))
    if choice is None:
        return None
    updated["windows_input"] = input_modes[choice]
    for name in ("tic_budget", "wall_seconds", "token_limit"):
        answer = int_field(name, updated[name])
        if answer is BACK:
            return None
        updated[name] = answer
    save_preset(LAUNCH_PRESET, updated)
    return updated


def launch_quick_development(scenario, *, agent="claude"):
    try:
        values = load_preset(LAUNCH_PRESET)
    except ValueError as exc:
        print(str(exc)); pause(); return
    while True:
        error = None
        try:
            validate_preset(values)
            if not resolve_path(REPO, values["profile"]).is_file():
                raise ValueError("WSL2 profile.json is missing; edit settings.")
        except ValueError as exc:
            error = str(exc)
        choice = menu(f"WSL2 {agent}: startup summary", [
            {"label": "Preview development", "disabled": error is not None},
            {"label": "Edit saved settings"},
            {"label": "Advanced: manual setup"},
            {"label": "Back"},
        ], subtitle=error or f"{scenario.title} · {values['model']}")
        if choice is None or choice == 3:
            return
        if choice == 1:
            try:
                updated = _edit_launch_preset(values)
                if updated is not None:
                    values = updated
            except (ValueError, OSError):
                print("Could not save valid launcher settings."); pause()
            continue
        if choice == 2:
            launch_manual_development(scenario, agent=agent)
            return
        if choice != 0 or error is not None:
            return
        preview_and_run_launcher("run_code.ps1", [
            "-Games", scenario.slug, "-Agent", agent, "-Models", values["model"],
            "-Profile", values["profile"], "-ProviderConfig", values["credentials"],
            "-WindowsInput", values["windows_input"], "-TicBudget", str(values["tic_budget"]),
            "-WallSeconds", str(values["wall_seconds"]), "-TokenLimit", str(values["token_limit"]),
        ], f"Launch this real-token {agent} run?")
        return


def launch_manual_development(scenario, *, agent="claude"):
    fields = _current_program_fields(scenario)
    if fields is None:
        return
    model = field(f"{agent} model", "", allow_back=True)
    if model is BACK:
        return
    upstream = field("Upstream HTTPS base URL (no key)", "", allow_back=True)
    if upstream is BACK:
        return
    key_env = field("Host credential environment-variable NAME", "OPENAI_API_KEY", allow_back=True)
    if key_env is BACK:
        return
    tokens = int_field("Model token stop threshold (post-response usage)", 30000)
    if tokens is BACK:
        return
    plan = DevelopmentPlan(**fields,model=model,upstream_url=upstream,
                           api_key_env=key_env,token_limit=tokens,
                           agent=agent)
    try:
        args = build_development_args(plan,execute=True)
    except ValueError as exc:
        print(f"Invalid development plan: {exc}"); pause(); return
    print(command_text([sys.executable,"-m",BENCHMARK_MODULE,*args]))
    print(f"Real {agent} via host gateway: model={model}; upstream={upstream}; key source env={key_env}.")
    print(f"Wall deadline={fields['wall_seconds']}s; game budget={fields['tic_budget']} tics; model budget={tokens} tokens.")
    print("Post-response usage accounting: the final request may exceed the token threshold.")
    print("Missing usage remains unknown; CLI recovery can incur further cost.")
    print("Exact currency cost is unknown. No input_tokens endpoint required.")
    print("Creates a clean WSL2 clone; may restore saves and launch/close the game.")
    print("Final test is separate. The runtime exports evidence before clone cleanup.")
    if confirm(f"Launch this real-token {agent} development run?",default=False):
        run_python_module(BENCHMARK_MODULE,args)


# ---------------------------------------------------------------- recording / calibration hub
def select_driver_game(title: str, *, include_invalid: bool = False) -> str | None:
    files = game_files()
    if not files:
        clear(); banner(); print(f"  {RED}No driver YAML files in {GAMES_DIR}.{R}")
        pause()
        return None
    items = []
    for path in files:
        ok, reason = validate(path)
        items.append({
            "label": path.stem,
            "icon": "🎮" if ok else "🔧",
            "tag": "valid" if ok else "invalid",
            "tag_color": GREEN if ok else RED,
            "hint": "" if ok else reason,
            "disabled": not ok and not include_invalid,
            "_slug": path.stem,
        })
    selected = menu(title, items, subtitle="driver configs under games/*.yaml")
    return None if selected is None else items[selected]["_slug"]


def launch_checks_menu(slug: str) -> None:
    while True:
        selected = menu(f"Launch checks · {slug}", [
            {"label": "Dry launch", "icon": "🚀", "hint": "does the window come up?",
             "tag": "test_auto_launch_dry"},
            {"label": "Calibrate", "icon": "📸",
             "hint": "15 screenshots @ 4s to read the menu path",
             "tag": "test_auto_launch_calibrate"},
            {"label": "Full auto-launch", "icon": "🧭",
             "hint": "settle -> nav -> join, 3 screenshots",
             "tag": "test_auto_launch_full"},
        ], subtitle="no LLM calls; these may start and close the real game")
        if selected is None:
            return
        if selected == 0:
            run_script("calibration/test_auto_launch_dry.py", ["--game", slug])
        else:
            scenario = select_benchmark_scenario(
                "Choose the scenario for this launch check",
                engine="windows", driver_game=slug,
            )
            if scenario is None:
                continue
            script = "test_auto_launch_calibrate.py" if selected == 1 else "test_auto_launch_full.py"
            run_script(f"calibration/{script}", ["--scenario", scenario.slug])
        close_or_pause(slug)


def recording_menu() -> None:
    while True:
        selected = menu("Record & calibrate", [
            {"label": "Record menu navigation", "icon": "🎬",
             "tag": "menu_skip", "tag_color": GREEN,
             "hint": "cold start → human pad demo → update driver YAML"},
            {"label": "Record scorer calibration", "icon": "🎥",
             "tag": "Windows scenario", "tag_color": PURPLE,
             "hint": "cold start → menu_skip → human play capture"},
            {"label": "Run auto-launch checks", "icon": "🧭",
             "tag": "diagnose", "tag_color": CYAN,
             "hint": "dry launch, screenshot calibration, full launch"},
            {"label": "Validate driver config", "icon": "✓",
             "tag": "YAML", "tag_color": GREEN,
             "hint": "parse and validate one games/*.yaml file"},
        ], subtitle="recording first · no LLM calls")
        if selected is None:
            return
        if selected == 0:
            scenario = select_benchmark_scenario(
                "Record menu navigation · pick a scenario", engine="windows")
            if scenario:
                record_startup(scenario)
        elif selected == 1:
            scenario = select_benchmark_scenario(
                "Record scorer calibration · pick a scenario", engine="windows")
            if scenario:
                record_scorer_calibration(scenario.slug)
        elif selected == 2:
            slug = select_driver_game("Auto-launch checks · pick a game")
            if slug:
                launch_checks_menu(slug)
        elif selected == 3:
            slug = select_driver_game("Validate driver config · pick a game", include_invalid=True)
            if not slug:
                continue
            ok, msg = validate(GAMES_DIR / f"{slug}.yaml")
            clear(); banner()
            mark = f"{GREEN}✓ valid{R}" if ok else f"{RED}✗ {msg}{R}"
            print(f"  {B}{slug}{R}: {mark}")
            pause()


def main_menu_items() -> list[dict]:
    return [
        {"label": "Run experiment", "icon": "🧪", "run": launch_experiment,
         "hint": "development and the Direct baseline"},
        {"label": "Independent final tests", "icon": "#", "run": final_tests_menu,
         "hint": "test a selected program or fill missing repeats"},
        {"label": "Record & calibrate", "icon": "🎥",
         "hint": "menu navigation, scorer evidence, launch checks", "run": recording_menu},
        {"label": "Host setup and diagnostics", "icon": "⚙", "run": host_setup_menu,
         "hint": "prepare dependencies and check prerequisites"},
        {"label": "Quit", "icon": "👋"},
    ]


# ---------------------------------------------------------------- main loop
def main() -> int:
    while True:
        items = main_menu_items()
        sel = menu(
            "What do you want to do?",
            items,
        )
        action = None if sel is None else items[sel].get("run")
        if action is None:
            clear()
            print(f"  {ACCENT}bye{R} 👋\n")
            return 0
        action()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        show_cursor()
        print(f"\n  {ACCENT}bye{R}")
        sys.exit(0)
    finally:
        show_cursor()
