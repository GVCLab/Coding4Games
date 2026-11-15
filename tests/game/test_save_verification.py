from pathlib import Path
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows.save_verification import SaveVerificationMonitor
from coding4game_gui.benchmark.windows.save_verification import SaveVerificationSpec
from coding4game_gui.benchmark.windows.save_verification import parse_save_verification_spec


def _spec(tmp_path: Path, **overrides) -> SaveVerificationSpec:
    values = dict(
        implementation="palworld_capture",
        poll_interval_seconds=1.0,
        stable_seconds=2.0,
        timeout_seconds=90.0,
        toolkit_root=tmp_path,
        target_character_ids=("BOSS_CaptainPenguin", "Penguin"),
        candidate_actions=("RB",),
        consumable_item_id="",
    )
    values.update(overrides)
    return SaveVerificationSpec(**values)


def test_parse_save_verification_configuration(tmp_path, monkeypatch):
    raw = {
        "save_verification": {
            "implementation": "palworld_capture",
            "poll_interval_seconds": 0.5,
            "stable_seconds": 1.5,
            "timeout_seconds": 45,
            "toolkit_root": str(tmp_path),
            "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
            "candidate_actions": ["rb"],
            "consumable_item_id": "PalSphere_Exotic",
        }
    }
    parsed = parse_save_verification_spec(raw)
    assert parsed.poll_interval_seconds == 0.5
    assert parsed.stable_seconds == 1.5
    assert parsed.timeout_seconds == 45.0
    assert parsed.target_character_ids == ("BOSS_CaptainPenguin", "Penguin")
    assert parsed.candidate_actions == ("RB",)
    assert parsed.consumable_item_id == "PalSphere_Exotic"


@pytest.mark.parametrize("field,value", [
    ("poll_interval_seconds", 0),
    ("stable_seconds", -1),
    ("timeout_seconds", -1),
    ("timeout_seconds", float("inf")),
])
def test_invalid_save_verification_timing_fails(field, value, tmp_path):
    raw = {"save_verification": {
        "implementation": "palworld_capture",
        "toolkit_root": str(tmp_path),
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        field: value,
    }}
    with pytest.raises(ValueError):
        parse_save_verification_spec(raw)


def test_zero_timeout_disables_wall_clock_expiry(tmp_path):
    raw = {"save_verification": {
        "implementation": "palworld_capture",
        "toolkit_root": str(tmp_path),
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        "timeout_seconds": 0,
    }}
    parsed = parse_save_verification_spec(raw)
    assert parsed.timeout_seconds == 0.0
    assert parsed.candidate_actions == ()


def test_positive_timeout_requires_candidate_action(tmp_path):
    raw = {"save_verification": {
        "implementation": "palworld_capture",
        "toolkit_root": str(tmp_path),
        "target_character_ids": ["Penguin"],
        "timeout_seconds": 30,
        "candidate_actions": [],
    }}
    with pytest.raises(ValueError, match="nonempty"):
        parse_save_verification_spec(raw)


def test_retired_capture_count_is_rejected(tmp_path):
    lifecycle = {"save_verification": {
        "implementation": "palworld_capture",
        "toolkit_root": str(tmp_path),
        "required_new_palbox_count": 1,
        "target_character_ids": ["Penguin"],
    }}
    with pytest.raises(ValueError, match="Retired save verification options"):
        parse_save_verification_spec(lifecycle)


def test_monitor_detects_new_palbox_instances_from_durable_save(tmp_path):
    save_root = tmp_path / "world"
    players = save_root / "Players"
    players.mkdir(parents=True)
    level = save_root / "Level.sav"
    player = players / "0001.sav"
    level.write_bytes(b"baseline")
    player.write_bytes(b"player")
    snapshots = [
        {"target_instances": [
            {"instance_id": "old-king", "character_id": "BOSS_CaptainPenguin",
             "in_palbox": True},
            {"instance_id": "old-small", "character_id": "Penguin",
             "in_palbox": True},
        ]},
        {"target_instances": [
            {"instance_id": "old-king", "character_id": "BOSS_CaptainPenguin",
             "in_palbox": True},
            {"instance_id": "old-small", "character_id": "Penguin",
             "in_palbox": True},
            {"instance_id": "new-king", "character_id": "BOSS_CaptainPenguin",
             "in_palbox": True},
            {"instance_id": "new-small", "character_id": "Penguin",
             "in_palbox": True},
        ]},
    ]

    def inspect(_level, _player):
        return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]

    monitor = SaveVerificationMonitor(
        _spec(tmp_path, poll_interval_seconds=60.0), save_root,
        inspector=inspect)
    monitor.start()
    try:
        monitor.note_action(["RB"], 606)
        level.write_bytes(b"captured")
        result = monitor.scan_once(force=True)
        assert result["status"] == "success"
        assert "candidate_tic" not in result
        assert monitor.completed
        assert result["captured_target_character_ids"] == [
            "BOSS_CaptainPenguin", "Penguin"]
        assert {item["instance_id"] for item in result["new_instances"]} == {
            "new-king", "new-small"}
    finally:
        monitor.stop()


def test_monitor_reports_consumed_inventory_item_count(tmp_path):
    save_root = tmp_path / "world"
    players = save_root / "Players"
    players.mkdir(parents=True)
    level = save_root / "Level.sav"
    player = players / "0001.sav"
    level.write_bytes(b"baseline")
    player.write_bytes(b"player")
    snapshots = [
        {"inventory_items": {"PalSphere_Exotic": 100},
         "target_instances": []},
        {"inventory_items": {"PalSphere_Exotic": 96},
         "target_instances": [
             {"instance_id": "new-king",
              "character_id": "BOSS_CaptainPenguin", "in_palbox": True},
             {"instance_id": "new-small", "character_id": "Penguin",
              "in_palbox": True},
         ]},
    ]

    def inspect(_level, _player):
        return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]

    monitor = SaveVerificationMonitor(
        _spec(tmp_path, poll_interval_seconds=60.0,
              consumable_item_id="PalSphere_Exotic"),
        save_root, inspector=inspect)
    monitor.start()
    try:
        level.write_bytes(b"captured")
        result = monitor.scan_once(force=True)
        assert result["consumable_baseline_count"] == 100
        assert result["consumable_current_count"] == 96
        assert result["consumable_consumed_count"] == 4
    finally:
        monitor.stop()


def test_monitor_uses_candidate_throw_count_when_inventory_save_lags(tmp_path):
    save_root = tmp_path / "world"
    players = save_root / "Players"
    players.mkdir(parents=True)
    level = save_root / "Level.sav"
    player = players / "0001.sav"
    level.write_bytes(b"baseline")
    player.write_bytes(b"player")
    snapshots = [
        {"inventory_items": {"PalSphere_Exotic": 100},
         "target_instances": []},
        {"inventory_items": {"PalSphere_Exotic": 100},
         "target_instances": [
             {"instance_id": "new-king",
              "character_id": "BOSS_CaptainPenguin", "in_palbox": True},
             {"instance_id": "new-small", "character_id": "Penguin",
              "in_palbox": True},
         ]},
    ]

    def inspect(_level, _player):
        return snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]

    monitor = SaveVerificationMonitor(
        _spec(tmp_path, poll_interval_seconds=60.0,
              consumable_item_id="PalSphere_Exotic"),
        save_root, inspector=inspect)
    monitor.start()
    try:
        monitor.note_action(["RB"], 10)
        monitor.note_action(["RB", "RS[0.1,0]"], 20)
        monitor.note_action(["RB"], 30)
        level.write_bytes(b"captured")
        result = monitor.scan_once(force=True)
        assert result["consumable_inventory_consumed_count"] == 0
        assert result["consumable_candidate_action_count"] == 3
        assert result["consumable_consumed_count"] == 3
    finally:
        monitor.stop()


def test_monitor_rejects_new_target_not_in_palbox(tmp_path):
    save_root = tmp_path / "world"
    players = save_root / "Players"
    players.mkdir(parents=True)
    level = save_root / "Level.sav"
    player = players / "0001.sav"
    level.write_bytes(b"baseline")
    player.write_bytes(b"player")
    calls = 0

    def inspect(_level, _player):
        nonlocal calls
        calls += 1
        return {"target_instances": ([
            {"instance_id": "wild", "in_palbox": False},
        ] if calls > 1 else [])}

    monitor = SaveVerificationMonitor(
        _spec(tmp_path, poll_interval_seconds=60.0), save_root,
        inspector=inspect)
    monitor.start()
    try:
        level.write_bytes(b"changed")
        assert monitor.scan_once(force=True) is None
        assert monitor.result is None
    finally:
        monitor.stop()


def test_monitor_does_not_finish_until_every_required_species_is_new(tmp_path):
    save_root = tmp_path / "world"
    players = save_root / "Players"
    players.mkdir(parents=True)
    level = save_root / "Level.sav"
    (players / "0001.sav").write_bytes(b"player")
    level.write_bytes(b"baseline")
    calls = 0

    def inspect(_level, _player):
        nonlocal calls
        calls += 1
        return {"target_instances": ([] if calls == 1 else [
            {"instance_id": "new-king", "character_id": "BOSS_CaptainPenguin",
             "in_palbox": True},
        ])}

    monitor = SaveVerificationMonitor(
        _spec(tmp_path, poll_interval_seconds=60.0), save_root,
        inspector=inspect)
    monitor.start()
    try:
        level.write_bytes(b"only-one-species")
        assert monitor.scan_once(force=True) is None
        assert monitor.result is None
        assert monitor.progress["status"] == "partial"
        assert monitor.progress["captured_target_character_ids"] == [
            "BOSS_CaptainPenguin"]
    finally:
        monitor.stop()


def _score_env(verification_result, progress=None):
    from coding4game_gui.benchmark.windows.env import WindowsGameEnv

    env = object.__new__(WindowsGameEnv)
    env._save_verifier = SimpleNamespace(
        final_check=lambda: verification_result,
        result=verification_result,
        progress=(progress if progress is not None else verification_result),
    )
    env._verified_completion = None
    env._first_capture_tic = None
    env._completion_tic = None
    env._tic = 900
    env._episode_id = 0
    env._done = False
    env.end_reason = None
    env._deferred_score = SimpleNamespace(settle=lambda _scorer: 1.0)
    env._scorer = object()
    env._total = 0.0
    env._score_breakdown = None
    env.spec = {"scorer": {"capture_progress": {
        "max_tics": 2000,
        "tics_per_second": 10,
        "points_per_target": 30,
        "first_capture_speed_points": 10,
        "full_completion_speed_points": 30,
        "waste_penalty_per_item": 2,
        "max_waste_penalty": 20,
    }}}
    return env


def test_visual_match_alone_cannot_pass_save_verified_task():
    env = _score_env(None)
    assert env.finalize_score() == 0.0
    assert env.score_breakdown["complete"] is False
    assert env.score_breakdown["captured_target_count"] == 0


def test_verified_save_is_scored_at_host_verification_tic():
    result = {
        "status": "success",
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        "captured_target_character_ids": [
            "BOSS_CaptainPenguin", "Penguin"],
        "new_instances": [{"instance_id": "new"}],
    }
    env = _score_env(result)
    assert env.finalize_score() == pytest.approx(82.0)
    assert env.done
    assert env.score_breakdown["completion_tics"] == 900
    assert env.score_breakdown["save_verification"] == dict(
        result, first_capture_tic=900, verified_tic=900)


def test_one_saved_target_scores_partial_credit_with_first_capture_speed():
    progress = {
        "status": "partial",
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        "captured_target_character_ids": ["Penguin"],
        "new_instances": [{"instance_id": "new-small"}],
    }
    env = _score_env(None, progress=progress)

    assert env.finalize_score() == pytest.approx(35.5)
    assert env.score_breakdown["complete"] is False
    assert env.score_breakdown["completion_tics"] is None
    assert env.score_breakdown["first_capture_tics"] == 900
    assert env.score_breakdown["first_capture_speed_points"] == 5.5
    assert env.score_breakdown["completion_speed_points"] == 0.0


def test_two_capture_milestones_use_independent_tics():
    progress = {
        "status": "success",
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        "captured_target_character_ids": [
            "BOSS_CaptainPenguin", "Penguin"],
        "first_capture_tic": 200,
        "new_instances": [{"instance_id": "new-king"},
                          {"instance_id": "new-small"}],
    }
    env = _score_env(progress)
    env._tic = 600

    assert env.finalize_score() == pytest.approx(90.0)
    assert env.score_breakdown["first_capture_speed_points"] == pytest.approx(9.0)
    assert env.score_breakdown["completion_speed_points"] == pytest.approx(21.0)


def test_extra_consumed_spheres_are_penalized_but_required_ones_are_free():
    result = {
        "status": "success",
        "target_character_ids": ["BOSS_CaptainPenguin", "Penguin"],
        "captured_target_character_ids": [
            "BOSS_CaptainPenguin", "Penguin"],
        "new_instances": [{"instance_id": "new-king"},
                          {"instance_id": "new-small"}],
        "consumable_item_id": "PalSphere_Exotic",
        "consumable_consumed_count": 5,
    }
    env = _score_env(result)

    assert env.finalize_score() == pytest.approx(76.0)
    assert env.score_breakdown["necessary_consumable_count"] == 2
    assert env.score_breakdown["wasted_consumable_count"] == 3
    assert env.score_breakdown["waste_penalty"] == 6


def test_verified_save_interrupts_current_action_and_uses_stop_tic():
    from PIL import Image

    from coding4game_gui.benchmark.windows.env import WindowsGameEnv
    from coding4game_gui.benchmark.windows.games_config import ActionSpec

    class Verifier:
        def __init__(self):
            self.result = None

        @property
        def completed(self):
            return self.result is not None

        def start(self):
            pass

        def stop(self):
            pass

        def note_action(self, _buttons, _tic):
            pass

        def final_check(self):
            return self.result

    verifier = Verifier()

    class Driver:
        last_step_timing = None

        def render(self):
            return Image.new("RGB", (32, 24))

        def step(self, **kwargs):
            assert not kwargs["should_stop"]()
            verifier.result = {
                "status": "success",
                "target_character_ids": [
                    "BOSS_CaptainPenguin", "Penguin"],
                "captured_target_character_ids": [
                    "BOSS_CaptainPenguin", "Penguin"],
                "new_instances": [{"instance_id": "new"}],
            }
            assert kwargs["should_stop"]()
            self.last_step_timing = SimpleNamespace(
                requested_s=30.0,
                live_window_s=5.4,
                interrupted=True,
                control_transition_s=0.0,
                timing_source="test",
            )
            return self.render()

    spec = {
        "engine": "windows",
        "runtime": {"scenario": "capture", "tics_per_second": 10},
        "lifecycle": {"terminal_detection": False},
        "scorer": {"capture_progress": {
            "max_tics": 1800,
            "tics_per_second": 10,
            "points_per_target": 30,
            "full_completion_speed_points": 40,
        }},
    }
    scorer = SimpleNamespace(
        reset=lambda: None,
        is_terminal=lambda _frame: False,
        classify_phase=lambda _frame: "pending",
    )
    env = WindowsGameEnv(
        "capture",
        driver_env=Driver(),
        scorer=scorer,
        actions={"NEUTRAL": ActionSpec(name="neutral", description="")},
        spec=spec,
        save_verifier=verifier,
    )
    env.reset()

    env.act(["NEUTRAL"], 300)

    assert env.done
    assert env.tic == 54
    assert env.last_action_timing["interrupted"] is True
    assert env._deferred_score is None
    assert env.finalize_score() == pytest.approx(98.8)
    assert env.score_breakdown["completion_tics"] == 54
