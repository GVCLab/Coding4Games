"""Composite scoring rules and native image calibration; no game or model launch."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from coding4game_gui.benchmark.windows.scorers.overcooked_process import (
    ProcessLedger, ProcessObserver, Story21Detector, VisualFact, breakdown,
)


def fact(recipe, ingredient, stage, frame=1, attempt="one"):
    return VisualFact(attempt, recipe, ingredient, stage, frame,
                      "native_sample", (500, 300, 40, 40), "visible station state")


def test_zero_and_single_ingredient_milestones():
    ledger = ProcessLedger()
    assert ledger.result() == (0, [])
    for stage, expected in (("upper_raft", 4), ("chopped", 8), ("fried", 14)):
        assert ledger.add(fact("fries", "potato", stage))
        assert ledger.result()[0] == expected
    assert ledger.add(fact("fries", None, "plated"))
    assert ledger.result()[0] == 20
    assert not ledger.add(fact("fries", None, "plated"))
    assert ledger.result()[0] == 20


def test_combo_one_ingredient_split_plates_and_correct_shared_plate():
    ledger = ProcessLedger()
    for stage, expected in (("upper_raft", 2), ("chopped", 4), ("fried", 7)):
        ledger.add(fact("chicken_with_fries", "chicken", stage))
        assert ledger.result()[0] == expected
    ledger.add(fact("chicken_with_fries", "potato", "fried", 2))
    assert ledger.result()[0] == 14
    # Cooked ingredients on separate plates do not prove the shared combo plate.
    assert ledger.result()[0] == 14
    combo = ProcessLedger()
    combo.add(fact("chicken_with_fries", "chicken", "fried"))
    combo.add(fact("chicken_with_fries", "potato", "fried", 2))
    assert combo.result()[0] == 14
    combo.add(fact("chicken_with_fries", None, "plated", 3))
    assert combo.result()[0] == 20


def test_later_state_implies_earlier_without_repeated_credit():
    ledger = ProcessLedger()
    ledger.add(fact("fried_chicken", "chicken", "fried", 12))
    score, evidence = ledger.result()
    assert score == 14
    assert [item["provenance"] for item in evidence] == [
        "inferred_from_later_state", "inferred_from_later_state", "direct"]
    assert {item["witness_frame_id"] for item in evidence} == {12}
    for frame in (13, 14, 15):
        assert not ledger.add(fact("fried_chicken", "chicken", "fried", frame))
    assert ledger.result()[0] == 14
    assert ledger.result(completed_orders=1)[0] == 20
    assert ledger.result(completed_orders=0)[0] == 14


def test_two_combo_orders_cannot_combine_different_attempts():
    ledger = ProcessLedger()
    ledger.add(fact("chicken_with_fries", "chicken", "fried", 10, "first"))
    ledger.add(fact("chicken_with_fries", "potato", "fried", 20, "second"))
    assert ledger.result()[0] == 7


@pytest.mark.parametrize("net,process,final,combined,success", [
    (-30, 4, 0, 4, False), (0, 0, 0, 0, False),
    (110, 8, 40, 48, False), (220, 20, 80, 100, True),
    (246, 20, 80, 100, True),
])
def test_versioned_breakdown(net, process, final, combined, success):
    row = breakdown(net, 220, process, valid_result=True)
    assert row["process_score"] == process
    assert row["final_component"] == final
    assert row["combined_score"] == combined
    assert row["game_score"] == min(100, 100 * net / 220)
    assert row["net_score"] == net
    assert row["success"] is success


def test_invalid_result_retains_diagnostic_progress_only():
    row = breakdown(None, 220, 7, valid_result=False)
    assert row["process_score"] == 7
    assert all(row[key] is None for key in (
        "combined_score", "final_component", "net_score", "game_score", "success"))


def test_direct_episode_ledger_uses_composite_and_excludes_invalid(tmp_path):
    import json
    from coding4game_gui.benchmark.direct.player import PlayEpisode
    from coding4game_gui.benchmark.direct.run_direct_baseline import _write_episode_rows

    scores = [breakdown(110, 220, 8, valid_result=True),
              breakdown(220, 220, 20, valid_result=True)]
    episodes = []
    for index, fields in enumerate(scores):
        episodes.append(PlayEpisode(
            episode_id=index, score=fields["combined_score"],
            score_version=fields["score_version"],
            process_score=fields["process_score"],
            final_component=fields["final_component"],
            combined_score=fields["combined_score"],
            net_score=fields["net_score"], game_score=fields["game_score"],
            success=fields["success"]))
    episodes.append(PlayEpisode(episode_id=2, score=7, tics=2.6, calls=1,
                                tokens_in=3, tokens_out=2, error="capture failed",
                                outcome_kind="infrastructure_error"))
    totals = _write_episode_rows(tmp_path, episodes, model="test",
                                 prompt_sha="new-prompt", scenario="overcooked_2_2_1")
    rows = [json.loads(line) for line in (tmp_path / "episodes.jsonl").read_text().splitlines()]
    assert [row["score"] for row in rows] == [48, 100, None]
    assert [row["success"] for row in rows] == [False, True, None]
    assert all(row["score_version"] == scores[0]["score_version"] for row in rows[:2])
    assert rows[-1]["partial_score"] == 7
    metrics = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    trials = [json.loads(line) for line in (tmp_path / "trials.jsonl").read_text().splitlines()]
    assert [row["score"] for row in metrics] == [48, 100, None]
    assert [row["episode_ids"] for row in trials] == [[0], [1], [2]]
    assert [row["per_episode_scores"] for row in trials] == [{"0": 48}, {"1": 100}, {}]
    assert all(row["policy_sha256"] == "new-prompt" for row in trials)
    assert trials[-1]["errors"] == ["capture failed"] and not trials[-1]["accepted"]
    assert metrics[-1]["env_tics_cum"] == trials[-1]["environment_steps_cum"] == totals.env_tics == 2.6
    assert metrics[-1]["tokens_in_cum"] == trials[-1]["tokens_in_cum"] == totals.tokens_in == 3


def test_current_prompts_describe_composite():
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.direct.player import build_direct_system
    from coding4game_gui.benchmark.code.prompts import render_development_prompt

    for input_kind in ("gamepad-kbm", "gamepad"):
        spec = load_game_spec("overcooked_2_2_1", windows_input=input_kind)
        prompts = [build_direct_system(spec, spec["runtime"].get("max_burst", 5)),
                   render_development_prompt(spec)]
        for prompt in prompts:
            assert prompt.count("80 * clamp(S / T, 0, 1)") == 1
            assert "best visually verified preparation progress" in prompt
            assert "Success still means S >= T" in prompt
            assert prompt.count("The kitchen has two separate moving rafts") == 1


def test_direct_baseline_mean_uses_composite_without_model_calls(tmp_path, monkeypatch):
    import json
    from coding4game_gui.benchmark.direct.player import PlayEpisode
    from coding4game_gui.benchmark.direct import run_direct_baseline as direct

    fields = [breakdown(110, 220, 8, valid_result=True),
              breakdown(220, 220, 20, valid_result=True)]
    played = [PlayEpisode(episode_id=i, score=part["combined_score"],
                          score_version=part["score_version"],
                          process_score=part["process_score"],
                          final_component=part["final_component"],
                          combined_score=part["combined_score"],
                          net_score=part["net_score"],
                          game_score=part["game_score"],
                          success=part["success"])
              for i, part in enumerate(fields)]
    monkeypatch.setattr(direct, "play_episodes", lambda *args, **kwargs: played)
    result = direct.run_baseline(
        out_dir=tmp_path / "run", scenario="overcooked_2_2_1", episodes=2,
        player_model="test-only", llm_fn=lambda *_: pytest.fail("model called"),
        make_env=lambda: pytest.fail("game launched"),
        prepare_windows=False, llm_log=False)
    assert result["baseline_mean"] == 74
    assert result["scored_episodes"] == 2
    config = json.loads((tmp_path / "run" / "config.json").read_text())
    assert config["score_version"] == fields[0]["score_version"]


class BrokenDetector:
    def detect(self, *_):
        raise RuntimeError("bad native frame")


def test_missing_or_corrupt_evidence_fails_closed(tmp_path):
    observer = ProcessObserver(BrokenDetector())
    observer.observe(np.zeros((1200, 1920, 3), np.uint8), "sample")
    with pytest.raises(RuntimeError, match="process evidence failed"):
        observer.result()
    other = ProcessObserver(BrokenDetector())
    with pytest.raises(RuntimeError, match="missing native process witness"):
        other.save(tmp_path, [{"witness_frame_id": 999}])
    other.result()


def test_corrupt_saved_process_witness_fails_closed(tmp_path, monkeypatch):
    class OneFact:
        def detect(self, _frame, frame_id, source):
            return [fact("fries", "potato", "upper_raft", frame_id)]

    observer = ProcessObserver(OneFact())
    observer.observe(np.zeros((1200, 1920, 3), np.uint8), "sample")
    _, evidence = observer.result()
    real_read = Path.read_bytes

    def damaged_read(path):
        if path.name.startswith("process_"):
            return b"damaged"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", damaged_read)
    with pytest.raises(RuntimeError, match="corrupt native process witness"):
        observer.save(tmp_path, evidence)


CURRENT_HUMAN = Path(__file__).resolve().parents[1] / "fixtures/overcooked_current_human"


@pytest.mark.parametrize("name,expected", [
    ("chopped_potato", {("potato", "chopped")}),
    ("plated_cooked_fries", {(None, "plated")}),
])
def test_current_human_potato_preparation_is_visible(name, expected):
    frame = cv2.imread(str(CURRENT_HUMAN / f"{name}.png"))
    assert frame is not None
    facts = Story21Detector().detect(frame, 1, "current_human_native")
    assert expected <= {(item.ingredient, item.stage) for item in facts}


@pytest.mark.parametrize("name", ["raw_potato_on_plate", "frying_not_ready", "empty_start"])
def test_current_human_incomplete_food_is_not_a_finished_dish(name):
    frame = cv2.imread(str(CURRENT_HUMAN / f"{name}.png"))
    assert frame is not None
    facts = Story21Detector().detect(frame, 1, "current_human_native")
    assert not any(item.stage in {"fried", "plated"} for item in facts)
    if name == "empty_start":
        assert not facts
