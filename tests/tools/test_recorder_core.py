"""Recorded input intervals before they are written to the driver YAML."""
from coding4game_gui.benchmark.windows.menu_recorder import PadStateTracker, Step


def _drive(t: PadStateTracker, events: list[tuple[float, int, str, str, str]]) -> None:
    """Apply a list of (t, pad, kind, token, action) events in order."""
    for ev in events:
        t.on_event(*ev)


def test_tracker_finalize_captures_trailing_wait():
    """The idle between the last input and SAVE (F12 → finalize) is part of the
    skip and must be recorded as a final wait step."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.12, 1, 'button', 'A', 'up'),
    ])
    # Operator watches the last transition settle ~3s, THEN hits SAVE at t=3.2.
    tr.finalize(3.2)
    assert len(tr.steps) == 2, f"expected press + trailing wait, got {[s.description for s in tr.steps]}"
    last = tr.steps[-1]
    assert last.buttons == [], f"trailing step should be a neutral wait, got {last.buttons}"
    assert 3.0 < last.duration_seconds < 3.2, f"trailing wait dur={last.duration_seconds}"


def test_tracker_finalize_no_trailing_wait_when_saved_promptly():
    """No spurious wait when SAVE comes right after the last release."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.20, 1, 'button', 'A', 'up'),
    ])
    tr.finalize(0.22)  # within WAIT_GAP_S of the release
    assert len(tr.steps) == 1, f"expected just the press, got {[s.description for s in tr.steps]}"


def test_tracker_p1_then_p2_emits_steps():
    """P1 A press/release, gap 0.8s, P2 A press/release → P1 step + wait + P2 step."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.20, 1, 'button', 'A', 'up'),
        (1.00, 2, 'button', 'A', 'down'),
        (1.20, 2, 'button', 'A', 'up'),
    ])
    assert len(tr.steps) == 3, (
        f"expected 3 steps, got {len(tr.steps)}: {[s.description for s in tr.steps]}"
    )
    assert tr.steps[0].pad == 1 and tr.steps[0].buttons == ['A']
    assert tr.steps[1].buttons == [] and 0.79 < tr.steps[1].duration_seconds < 0.81
    assert tr.steps[2].pad == 2 and tr.steps[2].buttons == ['A']


def test_tracker_preserves_short_neutral_gap_between_repeated_presses():
    """A short release must remain visible so replay sees two button edges."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.20, 1, 'button', 'A', 'up'),
        (0.25, 1, 'button', 'A', 'down'),  # 50 ms neutral gap
        (0.45, 1, 'button', 'A', 'up'),
    ])
    waits = [s for s in tr.steps if s.buttons == []]
    assert len(waits) == 1, f"short release gap was lost: {[s.description for s in tr.steps]}"
    assert waits[0].pad == 1
    assert waits[0].duration_seconds >= tr.MIN_NEUTRAL_DUR_S
    assert [s.buttons for s in tr.steps] == [['A'], [], ['A']]


def test_tracker_auto_repeat_dropped():
    """Multiple 'down' for the same key (OS auto-repeat) yield one step."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.05, 1, 'button', 'A', 'down'),  # repeat
        (0.10, 1, 'button', 'A', 'down'),  # repeat
        (0.30, 1, 'button', 'A', 'up'),
    ])
    assert len(tr.steps) == 1, f"expected 1 step, got {len(tr.steps)}"
    assert tr.steps[0].buttons == ['A']
    assert 0.29 < tr.steps[0].duration_seconds < 0.31


def test_tracker_combined_button_press():
    """P1 holds A and B together for 0.3s → a step with both buttons appears."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'button', 'A', 'down'),
        (0.05, 1, 'button', 'B', 'down'),
        (0.30, 1, 'button', 'A', 'up'),
        (0.30, 1, 'button', 'B', 'up'),
    ])
    has_combined = any(set(s.buttons) >= {'A', 'B'} for s in tr.steps)
    assert has_combined, (
        f"expected a step with both A and B held, got {[s.buttons for s in tr.steps]}"
    )


def test_tracker_trigger_press_uses_trigger_field():
    """P1 holds F (LT) for 0.3s → step with left_trigger=1.0 and empty buttons."""
    tr = PadStateTracker()
    _drive(tr, [
        (0.00, 1, 'trigger_l', 'LT', 'down'),
        (0.30, 1, 'trigger_l', 'LT', 'up'),
    ])
    assert len(tr.steps) == 1
    assert tr.steps[0].left_trigger == 1.0
    assert 'LT' not in tr.steps[0].buttons
    assert tr.steps[0].buttons == []


def test_tracker_accepts_true_analog_left_stick_axis():
    tr = PadStateTracker()
    tr.on_event(0.00, 1, 'stick_l_axis', (0.35, 0.82), 'set')
    tr.on_event(0.30, 1, 'stick_l_axis', (0.0, 0.0), 'set')

    assert len(tr.steps) == 1
    assert tr.steps[0].left_stick == (0.35, 0.82)
    assert tr.steps[0].buttons == []


def test_tracker_finalize_emits_dangling_state():
    """If finalize() is called while a key is still held, emit a final step."""
    tr = PadStateTracker()
    tr.on_event(0.00, 1, 'button', 'A', 'down')
    tr.finalize(0.50)
    assert len(tr.steps) == 1
    assert tr.steps[0].buttons == ['A']
    assert 0.49 < tr.steps[0].duration_seconds < 0.51


def test_tracker_checkpoint_prefixes_next_step():
    tr = PadStateTracker()
    tr.on_event(0.00, 1, 'button', 'A', 'down')
    tr.on_event(0.20, 1, 'button', 'A', 'up')
    tr.mark_checkpoint()
    tr.on_event(0.50, 1, 'button', 'B', 'down')
    tr.on_event(0.70, 1, 'button', 'B', 'up')
    has_marker = any("## CHECKPOINT:" in s.description for s in tr.steps)
    assert has_marker, (
        f"checkpoint not found in any description: {[s.description for s in tr.steps]}"
    )


def test_tracker_unknown_kind_raises():
    tr = PadStateTracker()
    raised = False
    try:
        tr.on_event(0.0, 1, 'bogus_kind', 'X', 'down')
    except ValueError:
        raised = True
    assert raised, "expected ValueError on unknown kind"


def test_tracker_invalid_pad_raises():
    tr = PadStateTracker()
    raised = False
    try:
        tr.on_event(0.0, 3, 'button', 'A', 'down')
    except ValueError:
        raised = True
    assert raised, "expected ValueError on pad != 1|2"


def test_tracker_instantaneous_tap_emits_step():
    """Tk can deliver KeyPress + KeyRelease in the same monotonic tick.
    The press must still produce a visible step (MIN_EMITTED_DUR_S floor)."""
    tr = PadStateTracker()
    tr.on_event(0.000, 1, 'button', 'A', 'down')
    tr.on_event(0.000, 1, 'button', 'A', 'up')
    assert len(tr.steps) == 1, (
        f"instant tap was lost (data leak); steps={[s.description for s in tr.steps]}"
    )
    assert tr.steps[0].buttons == ['A']
    assert tr.steps[0].duration_seconds >= 0.04, (
        f"expected MIN_EMITTED_DUR_S floor, got {tr.steps[0].duration_seconds}"
    )
    assert tr.data_lost is True, "data_lost flag should fire on coerced duration"


def test_tracker_simultaneous_cross_pad_press_emits_both():
    """P1 A + P2 A both pressed at t=0, both released at t=0.20.
    Both pad steps must appear (one with content), and overlap/data_lost flagged."""
    tr = PadStateTracker()
    tr.on_event(0.00, 1, 'button', 'A', 'down')
    tr.on_event(0.00, 2, 'button', 'A', 'down')
    tr.on_event(0.20, 1, 'button', 'A', 'up')
    tr.on_event(0.20, 2, 'button', 'A', 'up')
    # P1's span gets closed by P2's down (zero raw duration → floored).
    # P2's span ends at 0.20 (real duration).
    pad1_steps = [s for s in tr.steps if s.pad == 1 and s.buttons == ['A']]
    pad2_steps = [s for s in tr.steps if s.pad == 2 and s.buttons == ['A']]
    assert len(pad1_steps) >= 1, f"pad1 step lost: {[(s.pad, s.buttons, s.description) for s in tr.steps]}"
    assert len(pad2_steps) >= 1, f"pad2 step lost: {[(s.pad, s.buttons, s.description) for s in tr.steps]}"
    assert tr.data_lost is True


def test_tracker_clamps_out_of_order_timestamps():
    """If event timestamps go backwards, the tracker must not produce
    negative durations or corrupted last_change_t."""
    tr = PadStateTracker()
    tr.on_event(0.50, 1, 'button', 'A', 'down')
    tr.on_event(0.30, 1, 'button', 'A', 'up')  # earlier than press — clamped to 0.50
    # The press got clamped to 0.50, so raw duration = 0.50 - 0.50 = 0; floored to 0.05.
    assert len(tr.steps) == 1, (
        f"out-of-order should produce one floored step, got {[s.description for s in tr.steps]}"
    )
    assert tr.steps[0].duration_seconds >= 0.04


def test_step_validate_rejects_invalid_left_stick():
    for value in ((1.1, 0.0), (0.0, float("nan")), (0.0,), "up"):
        s = Step(pad=1, left_stick=value, duration_seconds=0.2)
        raised = False
        try:
            s.validate()
        except ValueError:
            raised = True
        assert raised, f"invalid left_stick should be rejected: {value!r}"


def test_step_validate_accepts_aliases_and_canonicals():
    Step(pad=1, buttons=['A'], duration_seconds=0.2).validate()  # alias
    Step(pad=1, buttons=['SOUTH'], duration_seconds=0.2).validate()  # canonical
    Step(pad=2, buttons=['LB', 'DPAD_LEFT'], duration_seconds=0.2).validate()
