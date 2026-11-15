"""Pure template rendering for open coding-agent development."""
from __future__ import annotations

import re
from string import Template

from coding4game_gui.benchmark.windows.raw_actions import windows_action_protocol
from coding4game_gui.benchmark.windows.timing import format_seconds_per_tic
from coding4game_gui.benchmark.windows.timing import resolve_tics_per_second
from coding4game_gui.benchmark.windows.timing import STEP_BUDGET_TOLERANCE_TICS
from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase


GUI_PROTOCOL = """Structured keyboard/mouse actions (CUA-style, not the native computer API):

### Complete action list

- click: button (left, right, or middle), x, y, tics; one click.
- double_click: x, y, tics; one left-button double click.
- move: x, y, tics; move the pointer and keep it there for hover.
- drag: path=[{x: start_x, y: start_y}, {x: end_x, y: end_y}], tics;
  hold the left mouse button while moving between the two points, then release.
- scroll: x, y, scroll_x, scroll_y, tics; scroll at the target position.
- keypress: keys=[...], tics; hold the listed physical keys simultaneously,
  then release them.
- wait: tics; advance the game without input.
- Screenshot: call game.observe separately; it does not advance game time
  and is not an action object for game.step.

### Observe and act at your chosen frequency

Call game.step(actions=[...]). The array is sequential; each object requires
type and integer tics from 1 to {max_action_tics}. One object gives one action/observation cycle;
multiple objects run without intermediate model decisions, then return one
final observation. game.observe takes a screenshot without advancing time.
Actions over {max_action_tics} tics are rejected before any input. A long hold stays continuously held,
not split into repeated presses. A click is still exactly one click; its tics
cover delivery plus settling, not mouse-button hold duration.
The host validates the entire batch before any input. Invalid batches cost
zero gameplay tics. A runtime failure cannot undo an already executed prefix.

### Current interface limits and pointer behavior

This is a custom subset, not a complete native desktop API. Do not assume
unsupported actions or combinations are available.
For captured-pointer games, the existing raw keyboard/mouse interface also accepts
`game.step(controls=["RMOVE[2,-1]"], tics=1)` and
`game.step(controls=["CLICK_CURRENT"], tics=1)`. These are syntax examples, not
calibrated movements. RMOVE emits one relative mouse event using signed integer
device units from -1000 to 1000; the two deltas cannot both be zero.
CLICK_CURRENT fires once without moving the captured pointer. Each pointer token
must be the sole control in its call; move and fire require separate calls.
Do not mix controls/tics with actions in one call. Structured actions remain
available; raw keyboard/mouse controls do not grant gamepad buttons or sticks.
- Drag accepts exactly two points: a straight-line left-button drag, not an
  arbitrary path. The complete gesture fits in the requested action time.
- Scroll amounts are signed WHEEL STEPS, not pixels;
  each amount is an integer from -10 to 10. Positive x scrolls right, positive
  y scrolls down. At least one amount must be nonzero.
- Keypress uses only the uppercase physical keys in the tool schema. This
  bounded hold is a game extension, not an instantaneous native keypress.
- No type-text action, modifier-plus-pointer combination or cross-step held-key
  state is provided. Do not use time.sleep to advance a frozen game.

Coordinates are integer pixels of the supplied 1280x768 screenshot: x=0..1279,
y=0..767. Never convert to desktop coordinates or silently clamp a target.
Click (left, right, or middle) and double-click release the button and leave
the pointer at the clicked target; moving it away requires an explicit move
action. Move leaves it for hover, drag releases and leaves it at the end, and
scroll leaves it at the target. Each action remains a
resume/input/release/freeze pulse. Host evidence is streamed during the hold
for recording only. Scores are settled after the episode ends.
{terminal_detection_text}
No per-action screenshot is returned to the
agent inside a batch. Do not assume later actions can react to unseen changes.

### Calling examples (syntax only, not a gameplay strategy)

These arbitrary coordinates and durations are not calibrated game targets or
recommended timings. Choose targets from your observations and durations for
your intended interaction. With the supplied Python client:

```python
client.call("game.observe")
client.call("game.step", actions=[
    {"type": "click", "button": "left", "x": 320, "y": 240, "tics": 2}
])
client.call("game.step", actions=[
    {"type": "drag", "path": [{"x": 320, "y": 240}, {"x": 640, "y": 240}], "tics": 5}
])
client.call("game.step", actions=[
    {"type": "move", "x": 400, "y": 300, "tics": 1},
    {"type": "scroll", "x": 400, "y": 300, "scroll_x": 0, "scroll_y": 1, "tics": 2}
])
```

Each step call above returns an observation after its executed actions. The
last example returns one final observation, not one after each object. You can
instead issue separate step calls when the next action depends on the result.

### Time accounting and interruption

The host checks limits before each action, may shorten the last action, and
stops the remaining batch on episode termination or exhausted resources.
Read batch.executed_actions, remaining_actions, stop_reason and each action's
requested_tics, allowed_tics, actual_tics and truncated fields. Cumulative
running seconds times the tic rate are rounded to the nearest integer, with
halves rounded up. Each actual_tics is the change in that cumulative counter,
not a separately rounded action. Requests and remaining budgets use integer tics.
The last executed action may exceed the limit by at most {overshoot_tics} tics without invalidating
the episode; actual usage is retained and no further action is allowed.
The timing object separates requested_seconds, game_running_seconds and driver
wall time. It is a host pause-boundary estimate, not an internal engine clock.
An interrupted or failed call may have consumed input: inspect status/evidence, do not blindly
repeat the batch. Gamepad control strings are unavailable in this input mode.
"""


def _gui_gameplay(text):
    """Keep public game knowledge, but name controls in the current GUI dialect."""
    names = {"CLICK": "click (left)", "RCLICK": "click (right)",
             "MCLICK": "click (middle)",
             "DOUBLE_CLICK": "double_click", "MOVE": "move", "DRAG": "drag",
             "SCROLL": "scroll", "KEY": "keypress"}
    text = re.sub(r"`(CLICK|RCLICK|MCLICK|DOUBLE_CLICK|MOVE|DRAG|SCROLL|KEY)\[([^`]*)\]`",
                  lambda m: "`" + names[m.group(1)] + " (" + m.group(2) + ")`", text)
    return text.replace("`NEUTRAL`", "`wait`").replace("ACT tics", "action tics")


def _terminal_detection_text(enabled: bool) -> str:
    if enabled:
        return (
            "The host checks the registered game-specific terminal conditions after each "
            "completed stepped action. A returned ended result "
            "with done=true is authoritative; do not send more input or call game.stop merely "
            "to close that ended episode. Stillness, unchanged screens, and ordinary intermediate "
            "states are not generic terminal conditions."
        )
    return (
        "Registered automatic terminal detection is disabled for this scenario. Explicitly "
        "stop an episode when appropriate; stillness or an unchanged screen does not end it."
    )


def _input_protocol(spec: dict, *, kbm: bool, terminal_detection: bool) -> str:
    terminal_text = _terminal_detection_text(terminal_detection)
    overshoot_tics = ({2: "two"}.get(STEP_BUDGET_TOLERANCE_TICS,
                                     str(STEP_BUDGET_TOLERANCE_TICS)))
    from coding4game_gui.benchmark.windows.timing import resolve_max_action_tics
    max_action_tics = resolve_max_action_tics(spec.get("runtime"))
    if kbm:
        return (GUI_PROTOCOL
                .replace("{terminal_detection_text}", terminal_text)
                .replace("{overshoot_tics}", overshoot_tics)
                .replace("{max_action_tics}", str(max_action_tics)))
    protocol = windows_action_protocol((spec.get("runtime") or {}).get("windows_input"))
    protocol = protocol.replace(
        "The last executed action may overrun the limit by up to one tic without invalidating",
        f"The last executed action may overrun the limit by up to {overshoot_tics} tics without invalidating",
    )
    protocol = protocol.replace("integer from 1 to 1000",
                                f"integer from 1 to {max_action_tics}")
    protocol = protocol.replace("per-action cap is 1000 tics",
                                f"per-action cap is {max_action_tics} tics")
    registered_text = (
        "Registered terminal screens are checked only after each action finishes; inactivity is not terminal."
    )
    return protocol.replace(registered_text, terminal_text)


def render_development_prompt(spec: dict, *, tic_budget=None, wall_seconds=None,
                              token_limit=None, max_tics=None, token_accounting="usage",
                              agent_name="claude",
                              runtime_backend="wsl2") -> str:
    """Consume selected public fields, explicit budgets and the shared protocol.

    The template is a task message for the selected CLI, not a replacement for its own
    base instructions. Never interpolate a complete config/spec into this text.
    """
    if token_accounting not in {"usage", "strict"}:
        raise ValueError("token_accounting must be usage or strict")
    if runtime_backend != "wsl2":
        raise ValueError("unsupported runtime backend")
    display_names = {"codex": "Codex", "claude": "Claude Code"}
    if agent_name not in display_names:
        raise ValueError("WSL2 development supports Claude Code and Codex only")
    display_name = display_names[agent_name]
    if runtime_backend == "wsl2":
        runtime_intro = (
            f"You are a real {display_name} process running as the run-specific "
            "unprivileged Linux user `c4gprobe` in one "
            "persistent-for-this-run WSL2 development environment, with the normal "
            f"{display_name} system prompt, Bash, file editing and other built-in coding tools."
        )
        development_environment = """You have standing authorization to write program files and install compatible
user-space packages or tools inside this development environment. Do not ask the
operator for permission before doing so. You have no sudo or administrator access.
Public downloads are permitted by policy during development, but actual package
routes and availability must be verified. Dependencies needed by the submitted
program are installed through the submission workflow; do not vendor a virtual
environment into the program bundle.

The verified baseline provides Python 3.13.5, OpenCV/cv2 4.10.0, NumPy 2.2.4,
Pillow 11.1.0, requests 2.32.3, Node.js 20, npm 9, Git 2.47 and curl 8.
The shared Python environment does not preinstall EasyOCR or PyTorch/torch.
CLI-bundled search and shell helpers vary with the selected baseline; verify their
actual paths and availability. These facts describe available resources, not a
preferred method. Verify any added dependency separately and
record its actual version. If one import in a compound probe fails, do not infer
that later, unexecuted imports also failed.

The run uses one clean clone of a pinned Linux baseline and has no per-rollout
environment or snapshots. A new development invocation creates a different clean
clone. Direct access to the Windows desktop is unavailable unless an explicit
host bridge provides that capability. Use the supplied game bridge for gameplay."""
        entrypoint_runtime = (
            'Submit a Python entrypoint with mode="stepped" and runtime="python".'
        )
        final_environment = """For additional Python packages or model assets, pass a
`dependencies` object containing `environment`. Its optional fields are
`requirements`, a list of exact `name==version` requirements; `indexes`, a list
of public HTTPS package-index URLs; and `assets`, a list of objects with `sha256`,
asset-relative `path`, and either a public HTTPS `url` or an absolute `source_path`
inside your development workspace. Keep large downloaded weights outside the
submitted code directory and declare them as assets. For example:

    {"environment": {"requirements": ["pyfiglet==1.0.2"],
      "indexes": ["https://pypi.org/simple"],
      "assets": [{"url": "https://example.org/model.bin",
                  "sha256": "<64 lowercase hex digits>",
                  "path": "models/model.bin"}]}}

On `submission.save`, the host resolves and downloads this declaration while
development networking is available, verifies hashes, and adds an immutable
`environment_lock` to the saved candidate identity. The dependency cache and
installed environment do not count against the program bundle limit. On
`submission.run`, the host prepares that exact locked environment before starting
the game, then runs the archived program with its prepared Python and
`C4G_ASSET_DIR`. A preparation failure consumes no game tics. The independently
invoked final test creates a fresh environment from the same lock before cutting
public network access; execution itself has no public network, coding agent, model
gateway, or inherited development environment. Do not provide `environment_lock`
yourself and do not rely on packages installed manually with Bash."""
        search_environment = (
            "Verify whether ripgrep (`rg`) is available on PATH or bundled with the selected CLI. "
            "Use available tools with scoped search paths and patterns; do not replace a failed search "
            "with an unbounded recursive listing."
        )
        development_shell_example = """In Bash, for example:

```bash
"$C4G_PYTHON" ./my_program/debug.py
```"""
    agent_tool_guidance = (
        "Host-controlled game.start and submission.run can take several minutes. "
        "Await these calls with the configured default yield time; do not override it "
        "with short yields and spend repeated model turns polling their progress. "
        "The original experiment deadlines still apply. "
        "If `apply_patch` is absent from the actual tool list, it is unavailable in "
        "this runtime; do not invoke it through a nested shell or PowerShell command. "
        "use an available direct shell/filesystem method instead. After two "
        "substantially identical file-write failures, change methods rather than "
        "repeating the same command or quoting pattern."
        if agent_name == "codex" else ""
    )
    runtime = spec.get("runtime") or {}
    kbm = runtime.get("windows_input") == "gamepad-kbm"
    terminal_detection = (spec.get("lifecycle") or {}).get("terminal_detection", True)
    if type(terminal_detection) is not bool:
        raise ValueError("lifecycle.terminal_detection must be a boolean")
    rate = resolve_tics_per_second(runtime)
    scenario_max = runtime.get("max_tics")
    limits = [value for value in (scenario_max, max_tics) if value is not None]
    maximum = min(limits) if limits else None
    rollout = (f"Each rollout is capped at {maximum} tics; max_tics may shorten "
               "but never increase that limit." if maximum is not None else
               "There is no independent per-rollout tic cap; the global "
               "interaction budget and normal episode termination still bind.")
    budgets = []
    for label, value, unit in (("Development interaction budget", tic_budget, "tics"),
                               ("Development wall-clock deadline", wall_seconds, "seconds"),
                               ("Model token budget", token_limit, "tokens")):
        if value is not None:
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
            budgets.append(f"- {label}: {value} {unit}.")
        elif label != "Development interaction budget":
            budgets.append(f"- {label}: unlimited; no configured {unit} limit.")
    if not budgets:
        budgets.append("Read bench.status for the configured development limits.")
    values = dict(
        game_title=spec.get("title", "Windows game"),
        agent_name=display_name,
        agent_tool_guidance=agent_tool_guidance,
        runtime_intro=runtime_intro,
        development_environment=development_environment,
        entrypoint_runtime=entrypoint_runtime,
        final_environment=final_environment,
        search_environment=search_environment,
        development_shell_example=development_shell_example,
        gameplay=(_gui_gameplay(spec.get("gameplay", "")) if kbm else spec.get("gameplay", "")),
        objective=spec.get("objective", ""),
        episode_contract=spec.get("episode_contract", ""),
        budget_text="\n".join(budgets), rollout_limit_text=rollout,
        token_accounting_text=((
            "Model tokens use post-response usage accounting. The host totals provider-reported "
            "input + output tokens after each response. If a token limit is configured, the final request may exceed the token "
            "threshold. Once it is reached, no further model request is allowed. "
            "bench.status.model_budget shows tokens_reported, tokens_remaining, tokens_overshoot "
            "and request_in_flight. When usage is complete and the limit is finite, "
            "estimated_model_requests_remaining divides known remaining tokens by "
            "last_settled_request_tokens; this latest-request estimate is advisory, not a "
            "future request-size or admission guarantee. token_breakdown reports confirmed "
            "input_tokens (including cache), "
            "cached_input_tokens, uncached_input_tokens and output_tokens (including reasoning). "
            "token_breakdown_unknown_requests counts missing data for each component; subtotals are not exact "
            "when these counts are nonzero. Cached tokens remain part of the total-token budget; these are "
            "token counts, not monetary charges. An in-flight request is not settled yet. Missing usage is "
            "marked unknown, never counted as zero. "
            + ("No token limit is configured; missing usage warns without stopping model access, and exact total cost is unknown. "
               if token_limit is None else
               "Missing usage warns without stopping model access; ordinary client recovery may continue. ")
            + "After any unknown usage, tokens_reported is a confirmed subtotal and "
            "tokens_remaining is unknown; the actual total may exceed any configured threshold. "
            + ("With no token limit, usage is still recorded but token consumption does not stop development."
               if token_limit is None else
               "With a finite usage-mode limit, the confirmed subtotal still stops later requests at the threshold, but unknown requests may already have exceeded it.")
            if token_accounting == "usage" else
            "Strict total-token accounting reserves a certified input count plus the output "
            "bound before each model request. Requests without a certified bound are refused.")
            + (" After confirmed usage first reaches 70% of the finite token limit, leaving "
               "approximately 30% for finalization, the host "
               "injects one finalization warning into the next admitted model request. The "
               "warning makes finalization mandatory: stop optional exploration, preserve or "
               "create a runnable candidate, complete only essential validation, and call "
               "submission.finish even if the best saved candidate remains imperfect. It does "
               "not extend either budget and cannot help if one response jumps directly past "
               "the limit."
               if token_limit is not None else "")),
        actions_table=(""
                       if kbm else "\n".join(f"- `{a['name']}`: {a['description']}"
                                             for a in spec.get("action_catalog", []))),
        input_protocol=_input_protocol(spec, kbm=kbm, terminal_detection=terminal_detection),
        terminal_tool_effect=(
            "Registered terminal detection ends and settles the episode when a game-specific "
            "ending is detected. If a receipt already reports done=true, no additional "
            "game.stop call is required. Stillness and unchanged screens are not generic endings."
            if terminal_detection else
            "Registered automatic terminal detection is disabled for this scenario; use "
            "game.stop when the episode should end. Stillness and unchanged screens do not "
            "end it on their own."
        ),
        step_usage=(""
                    if kbm else "The ACT notation describes controls/timing; call game.step(controls=[...], tics=N), not a chat ACT reply."),
        step_example=('client.call("game.step", actions=[{"type": "wait", "tics": 1}])'
                      if kbm else 'client.call("game.step", controls=["NEUTRAL"], tics=1)'),
        seconds_per_tic=format_seconds_per_tic(rate), tics_per_second=rate,
        tools_table="\n".join(
            f"- `{t['name']}`: " + (
                "Execute stepped actions using the protocol above and return an observation."
                if t['name'] == 'game.step' else t['description'].split('. ', 1)[0])
                              for t in tools_for_phase(
                                  "development",
                                  max_action_tics=(spec.get("runtime") or {}).get(
                                      "max_action_tics", 1000))),
    )
    from coding4game_gui.paths import development_template
    template = development_template()
    rendered = Template(template).substitute(values)
    if runtime_backend == "wsl2":
        rendered = rendered.replace(
            "Dependency declarations are not installation or verification.",
            "Environment declarations are resolved, hash-locked, installed and verified "
            "by the WSL2 submission workflow; other dependency metadata remains descriptive.",
        )
    return rendered
