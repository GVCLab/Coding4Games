"""Track virtual pad events and save recorded menu navigation to driver YAML."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.comments import CommentedSeq


@dataclass
class Step:
    """One recorded PadActionSpec entry, in the YAML shape parsed by
    `_parse_pad_action_entry` in coding4game_gui/benchmark/windows/games_config.py.

    `pad` is **1-indexed** (1 or 2) to match the YAML convention.
    For pure-wait steps, `buttons` is empty and `left_trigger` /
    `right_trigger` are zero — only `duration_seconds` is meaningful.
    """
    pad: int                                 # 1 or 2 (1-indexed)
    buttons: list[str] = field(default_factory=list)  # aliases: A, B, LB, DPAD_LEFT, ...
    left_stick: tuple[float, float] = (0.0, 0.0)
    left_trigger: float = 0.0                # 0.0 or 1.0 in v1 (binary)
    right_trigger: float = 0.0
    duration_seconds: float = 0.0
    description: str = ""

    def to_yaml_dict(self) -> dict:
        """Return the dict representation written into the YAML list."""
        out: dict = {"description": self.description}
        if self.buttons:
            out["buttons"] = list(self.buttons)
        if self.left_stick != (0.0, 0.0):
            out["left_stick"] = [round(axis, 2) for axis in self.left_stick]
        if self.left_trigger > 0.0:
            out["left_trigger"] = self.left_trigger
        if self.right_trigger > 0.0:
            out["right_trigger"] = self.right_trigger
        out["pad"] = self.pad
        out["duration_seconds"] = round(self.duration_seconds, 2)
        return out

    def validate(self) -> None:
        """Raise ValueError if this step would fail `_parse_pad_action_entry`
        or `_coerce_buttons` in games_config.py. Called by YamlPatcher.patch
        before writing — surfaces bugs at write time rather than next replay.
        """
        from coding4game_gui.benchmark.windows.gamepad import resolve_button
        if self.pad not in (1, 2):
            raise ValueError(f"Step.pad must be 1 or 2, got {self.pad!r}")
        if not isinstance(self.duration_seconds, (int, float)):
            raise ValueError(f"Step.duration_seconds must be numeric, got {self.duration_seconds!r}")
        if math.isnan(self.duration_seconds) or math.isinf(self.duration_seconds):
            raise ValueError(f"Step.duration_seconds must be finite, got {self.duration_seconds!r}")
        if self.duration_seconds < 0:
            raise ValueError(f"Step.duration_seconds must be >= 0, got {self.duration_seconds!r}")
        for btn in self.buttons:
            if resolve_button(btn) is None:
                raise ValueError(
                    f"Step.buttons contains unknown token {btn!r}; "
                    f"see benchmark/windows/gamepad.py for valid names"
                )
        try:
            stick = tuple(self.left_stick)
        except (TypeError, ValueError):
            raise ValueError(
                f"Step.left_stick must be a numeric [x,y] pair, got {self.left_stick!r}"
            ) from None
        if len(stick) != 2:
            raise ValueError(
                f"Step.left_stick must contain exactly 2 values, got {self.left_stick!r}"
            )
        for axis in stick:
            if not isinstance(axis, (int, float)) or not math.isfinite(axis):
                raise ValueError(
                    f"Step.left_stick values must be finite numbers, got {self.left_stick!r}"
                )
            if not -1.0 <= axis <= 1.0:
                raise ValueError(
                    f"Step.left_stick values must be in [-1,1], got {self.left_stick!r}"
                )
        if not (0.0 <= self.left_trigger <= 1.0):
            raise ValueError(f"Step.left_trigger must be in [0,1], got {self.left_trigger!r}")
        if not (0.0 <= self.right_trigger <= 1.0):
            raise ValueError(f"Step.right_trigger must be in [0,1], got {self.right_trigger!r}")


@dataclass
class _PadState:
    """Held buttons, analog inputs and last change time."""
    buttons: set[str] = field(default_factory=set)
    left_stick: tuple[float, float] = (0.0, 0.0)
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    last_change_t: Optional[float] = None  # monotonic seconds, or None until first event

    def is_neutral(self) -> bool:
        return (
            not self.buttons
            and self.left_stick == (0.0, 0.0)
            and self.left_trigger == 0.0
            and self.right_trigger == 0.0
        )

    def snapshot(self) -> tuple[frozenset[str], float, float, tuple[float, float]]:
        return (
            frozenset(self.buttons),
            self.left_trigger,
            self.right_trigger,
            self.left_stick,
        )


class PadStateTracker:
    """Serialize virtual pad changes into recorded menu steps.

    Recording rules:
      * Each pad has a held button set + analog triggers + `last_change_t`.
      * On any event that CHANGES a pad's state, emit a Step capturing what
        the pad was holding from its last change UNTIL NOW, using a
        pre-mutation snapshot so we report the state that was held, not the
        state we just changed to.
      * Preserve every neutral gap between acting spans. Gaps shorter than
        `MIN_NEUTRAL_DUR_S` are expanded so replay exposes at least one
        observable release interval to the game.
      * Repeated down events for a held button are ignored.
      * If both pads remain non-neutral simultaneously > `MIN_GAP_S` → set
        `overlap_detected` so the caller can warn.
    """

    # Only used to suppress insignificant idle time between the final input
    # and SAVE. Inter-action neutral gaps are never dropped: they carry the
    # release edge required by repeated menu presses.
    WAIT_GAP_S = 0.1
    MIN_GAP_S = 0.05
    # Minimum visible duration for any non-empty span. Instantaneous taps
    # (< 1 Tk tick) and simultaneous cross-pad presses both produce zero
    # raw duration; bump them to this so the user can see something happened.
    MIN_EMITTED_DUR_S = 0.05
    MIN_NEUTRAL_DUR_S = 0.05

    def __init__(self) -> None:
        self.pads: dict[int, _PadState] = {1: _PadState(), 2: _PadState()}
        self.steps: list[Step] = []
        # Snapshot of the pad's state DURING the currently-open span on each pad,
        # captured at the moment that span began (i.e., the state to emit when
        # the span closes).
        self._open_span: dict[
            int,
            Optional[tuple[frozenset[str], float, float, tuple[float, float]]],
        ] = {1: None, 2: None}
        # When each open span started — needed because pad.last_change_t advances
        # on intermediate same-pad changes that we close+reopen.
        self._span_start_t: dict[int, Optional[float]] = {1: None, 2: None}
        self._last_step_end_t: Optional[float] = None
        self._last_acting_pad: Optional[int] = None
        self._pending_checkpoint = False
        self.overlap_detected = False
        self.data_lost = False  # set when a span had to be coerced

    # ---------------- public API ----------------

    def on_event(self, t: float, pad: int, kind: str, token, action: str) -> None:
        if pad not in (1, 2):
            raise ValueError(f"pad must be 1 or 2, got {pad!r}")
        valid_actions = ('set',) if kind == 'stick_l_axis' else ('down', 'up')
        if action not in valid_actions:
            raise ValueError(
                f"action for {kind!r} must be {'/'.join(valid_actions)}, got {action!r}"
            )
        # Defensive: clamp backwards-going timestamps to monotonic ordering.
        # `time.monotonic()` shouldn't go backwards on Windows, but if events
        # are reordered (e.g., across threads), we must not corrupt the timeline.
        last_t = max((p.last_change_t for p in self.pads.values() if p.last_change_t is not None),
                     default=None)
        if last_t is not None and t < last_t:
            t = last_t
        ps = self.pads[pad]
        other = self.pads[3 - pad]
        before = ps.snapshot()
        if kind == 'button':
            if action == 'down':
                if token in ps.buttons:
                    return  # auto-repeat
                ps.buttons.add(token)
            else:
                ps.buttons.discard(token)
        elif kind == 'trigger_l':
            ps.left_trigger = 1.0 if action == 'down' else 0.0
        elif kind == 'trigger_r':
            ps.right_trigger = 1.0 if action == 'down' else 0.0
        elif kind == 'stick_l_axis':
            if (not isinstance(token, (list, tuple)) or len(token) != 2
                    or not all(isinstance(v, (int, float)) and math.isfinite(float(v))
                               for v in token)):
                raise ValueError(f"invalid left-stick axis {token!r}")
            x, y = float(token[0]), float(token[1])
            if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
                raise ValueError(f"left-stick axis out of range {token!r}")
            ps.left_stick = (x, y)
        else:
            raise ValueError(f"unknown kind {kind!r}")
        after = ps.snapshot()
        if before == after:
            return

        # Overlap detection.
        if not ps.is_neutral() and not other.is_neutral():
            if (other.last_change_t is not None
                    and t - other.last_change_t > self.MIN_GAP_S):
                self.overlap_detected = True

        now_neutral = ps.is_neutral()

        # If THIS pad had an open span, close it using the pre-mutation snapshot.
        if self._open_span[pad] is not None:
            self._close_span(pad, self._open_span[pad], self._span_start_t[pad], t)
            self._open_span[pad] = None
            self._span_start_t[pad] = None

        # If THE OTHER pad still has an open span and this pad is starting a
        # new acting span, close the other one too (sequentialization).
        if not now_neutral and self._open_span[3 - pad] is not None:
            self._close_span(3 - pad, self._open_span[3 - pad], self._span_start_t[3 - pad], t)
            self._open_span[3 - pad] = None
            self._span_start_t[3 - pad] = None

        # Update pad's last-change marker.
        ps.last_change_t = t

        # Open a new span on this pad if non-neutral.
        if not now_neutral:
            self._open_span[pad] = ps.snapshot()
            self._span_start_t[pad] = t

    def finalize(self, t: float) -> None:
        """On save, close any open spans, then capture the trailing
        idle between the last input and SAVE as a final wait step.

        That trailing pause IS part of the menu skip — it's the time the
        operator let the last transition / load finish before saving, and on
        replay the agent must wait it out too before taking over. Without this
        the recording ended at the last button press and the agent started too
        early."""
        for pad in (1, 2):
            if self._open_span[pad] is not None:
                self._close_span(pad, self._open_span[pad], self._span_start_t[pad], t)
                self._open_span[pad] = None
                self._span_start_t[pad] = None
        # Trailing wait: idle from the last emitted step's end until SAVE.
        if (self._last_step_end_t is not None
                and t - self._last_step_end_t > self.WAIT_GAP_S):
            wait_dur = t - self._last_step_end_t
            wait_pad = self._last_acting_pad if self._last_acting_pad else 1
            self._append_step(Step(
                pad=wait_pad,
                buttons=[],
                duration_seconds=wait_dur,
                description=f"wait {wait_dur:.2f}s (trailing — last input → SAVE)",
            ))
            self._last_step_end_t = t

    def mark_checkpoint(self) -> None:
        self._pending_checkpoint = True

    # ---------------- internal ----------------

    def _close_span(
        self,
        pad: int,
        snapshot: tuple[frozenset[str], float, float, tuple[float, float]],
        start_t: Optional[float],
        end_t: float,
    ) -> None:
        if start_t is None:
            return
        raw_duration = end_t - start_t
        has_content = (
            bool(snapshot[0])
            or snapshot[1] > 0
            or snapshot[2] > 0
            or snapshot[3] != (0.0, 0.0)
        )
        if not has_content:
            return  # nothing meaningful to record
        # Floor sub-tick spans to MIN_EMITTED_DUR_S so instantaneous taps and
        # cross-pad simultaneous presses both produce a visible step.
        if raw_duration < self.MIN_EMITTED_DUR_S:
            duration = self.MIN_EMITTED_DUR_S
            if raw_duration <= 0:
                # We're emitting a span that had zero real time — usually means
                # cross-pad simultaneity. Flag so the caller can warn the user.
                self.data_lost = True
        else:
            duration = raw_duration
        # Neutral step first if the previous acting span ended before this one
        # began. Even a very short release is semantically significant: without
        # it, adjacent presses serialize as one continuous hold on replay.
        # Floor it to a visible interval so the game gets a chance to poll the
        # neutral pad state between repeated presses.
        if self._last_step_end_t is not None and start_t > self._last_step_end_t:
            raw_wait_dur = start_t - self._last_step_end_t
            wait_dur = max(raw_wait_dur, self.MIN_NEUTRAL_DUR_S)
            wait_pad = self._last_acting_pad if self._last_acting_pad else pad
            suffix = (
                f" (recorded release gap {raw_wait_dur:.3f}s; "
                f"floored for replay)"
                if raw_wait_dur < self.MIN_NEUTRAL_DUR_S else ""
            )
            self._append_step(Step(
                pad=wait_pad,
                buttons=[],
                duration_seconds=wait_dur,
                description=f"wait {wait_dur:.2f}s{suffix}",
            ))
        buttons = sorted(snapshot[0])
        lt, rt, left_stick = snapshot[1], snapshot[2], snapshot[3]
        extras = []
        if buttons:
            extras.append(f"[{', '.join(buttons)}]")
        if lt:
            extras.append("LT")
        if rt:
            extras.append("RT")
        if left_stick != (0.0, 0.0):
            extras.append(f"LS({left_stick[0]:.2f},{left_stick[1]:.2f})")
        desc = f"recorded: pad{pad} " + " ".join(extras) + f" held {duration:.2f}s"
        self._append_step(Step(
            pad=pad,
            buttons=buttons,
            left_stick=left_stick,
            left_trigger=lt,
            right_trigger=rt,
            duration_seconds=duration,
            description=desc,
        ))
        self._last_step_end_t = end_t
        self._last_acting_pad = pad

    def _append_step(self, step: Step) -> None:
        if self._pending_checkpoint:
            step.description = "## CHECKPOINT: " + step.description
            self._pending_checkpoint = False
        self.steps.append(step)


class YamlPatcher:
    """Replace a driver's menu_skip with the recorded steps.

    Comments INSIDE the replaced list are lost — the per-step `description:`
    field is the surviving narration. Writes use a temporary sibling followed
    by `os.replace`; no persistent backup file is created.
    """

    def __init__(self, yaml_path: Path) -> None:
        if not yaml_path.exists():
            raise FileNotFoundError(yaml_path)
        self.yaml_path = yaml_path
        self._yaml = YAML(typ='rt')
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096  # don't auto-wrap long descriptions

    def patch(self, steps: list[Step]) -> dict:
        """Replace menu_skip and return the write summary.
          'speed_entries_dropped': int (count of `speed:` overrides lost)
          'steps_written': int
        Raises ValueError on bad input.
        """
        # Pre-validate every Step before touching the file — surfaces bugs at
        # write time rather than next replay.
        for i, s in enumerate(steps):
            try:
                s.validate()
            except ValueError as e:
                raise ValueError(f"steps[{i}]: {e}") from None

        with open(self.yaml_path, 'r', encoding='utf-8') as fh:
            data = self._yaml.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Game driver YAML must be a mapping")
        old_list = data.get("menu_skip") or []
        speed_drops = sum(
            1 for entry in old_list
            if isinstance(entry, dict) and 'speed' in entry
        )
        data["menu_skip"] = CommentedSeq(CommentedMap(step.to_yaml_dict()) for step in steps)

        # Atomic-ish write: dump to a sibling, then os.replace into place. Saves
        # the original from a partial-write on disk-full / power loss.
        tmp = self.yaml_path.with_suffix(self.yaml_path.suffix + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            self._yaml.dump(data, fh)
        os.replace(tmp, self.yaml_path)
        return {
            'speed_entries_dropped': speed_drops,
            'steps_written': len(steps),
        }
