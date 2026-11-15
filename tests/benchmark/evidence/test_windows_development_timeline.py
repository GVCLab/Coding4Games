import json
from pathlib import Path

import pytest

from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline


def test_timeline_records_sources_refs_and_scrubs_secrets(tmp_path):
    path = tmp_path / "development.jsonl"
    timeline = DevelopmentTimeline(path, secrets=["token-123"], max_inline_chars=32)
    first = timeline.append(
        "tool", source="harness", summary="download token-123",
        details={"command": "fetch token-123", "output": "x" * 100},
        refs=["observations/frame_1.jpg"],
    )
    note = timeline.self_note("I believe token-123 improved the policy")

    rows = timeline.read()
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[0]["source"] == "harness"
    assert rows[0]["refs"] == ["observations/frame_1.jpg"]
    assert "token-123" not in json.dumps(rows)
    assert "[REDACTED]" in rows[0]["summary"]
    assert rows[0]["details"]["output"].endswith("[TRUNCATED]")
    assert note["source"] == "agent_claim"
    assert first["kind"] == "tool"


def test_timeline_html_is_static_safe_and_states_evidence_limits(tmp_path):
    timeline = DevelopmentTimeline(tmp_path / "development.jsonl")
    timeline.append("observation", summary="<script>alert(1)</script>",
                    refs=["frames/frame.jpg"])
    timeline.submission({"id": "abc", "description": "<b>candidate</b>"}, event="saved")
    out = timeline.render_html(tmp_path / "timeline.html", submissions=[{
        "id": "abc", "description": "candidate", "entrypoint": {
            "argv": ["main.py"], "mode": "stepped", "runtime": "python"}}])

    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Agent-authored self-notes are unverified claims" in html
    assert "Dependency declarations are not proof" in html
    assert "cannot prove the absence of a local or embedded LLM" in html
    assert "abc" in html


@pytest.mark.parametrize("ref", [
    "/etc/passwd", "../secret.txt", "frames/../../secret.txt",
    r"C:\\secret.txt", "https://example.test/a.png", "javascript:alert(1)",
])
def test_timeline_rejects_nonlocal_or_escaping_evidence_refs(tmp_path, ref):
    with pytest.raises(ValueError, match="safe report-relative"):
        DevelopmentTimeline(tmp_path / "timeline.jsonl").append(
            "observation", refs=[ref])


def test_html_links_and_previews_only_existing_report_root_artifacts(tmp_path):
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "frame.png").write_bytes(b"png")
    (tmp_path / "evidence" / "episode.mp4").write_bytes(b"video")
    timeline = DevelopmentTimeline(tmp_path / "development.jsonl")
    timeline.append("observation", refs=[
        "evidence/frame.png", "evidence/episode.mp4", "evidence/missing.jpg"])

    out = timeline.render_html(tmp_path / "timeline.html")
    page = out.read_text(encoding="utf-8")
    assert '<a href="evidence/frame.png"' in page
    assert '<img src="evidence/frame.png"' in page
    assert '<video controls preload="metadata" src="evidence/episode.mp4"' in page
    assert 'href="evidence/missing.jpg"' not in page
    assert "evidence/missing.jpg" in page
    assert "https://" not in page and "javascript:" not in page


def test_html_indexes_full_codex_events_separately_from_truncated_summaries(tmp_path):
    full = {"type": "message", "text": "z" * 5000}
    (tmp_path / "codex_events.jsonl").write_text(
        json.dumps(full) + "\n", encoding="utf-8")
    timeline = DevelopmentTimeline(tmp_path / "development_timeline.jsonl",
                                   max_inline_chars=32)
    timeline.append("codex_event", source="sandbox", summary="z" * 5000,
                    details=full)
    page = timeline.render_html(tmp_path / "timeline.html").read_text(encoding="utf-8")
    assert 'href="codex_events.jsonl"' in page
    assert "1 full stdout event" in page
    assert "Timeline summaries are bounded and may be truncated" in page
    assert "z" * 5000 not in page


def _submission(root: Path, ident: str, source: str, *, verified=False):
    files = root / "programs" / ident / "files"
    files.mkdir(parents=True)
    (files / "main.py").write_text(source, encoding="utf-8")
    return {"id": ident, "description": f"candidate {ident}",
            "entrypoint": {"argv": ["main.py"], "mode": "stepped",
                           "runtime": "python"},
            "dependencies": {"python": "3.12"},
            "reproducibility": {"dependencies_verified": verified},
            "files": {"main.py": "fixture-sha"}}


def test_html_links_submission_files_and_labels_dependency_status_and_diffs(tmp_path):
    one = _submission(tmp_path, "one", "value = 1\n")
    two = _submission(tmp_path, "two", "value = 2\n", verified=True)
    timeline = DevelopmentTimeline(tmp_path / "development_timeline.jsonl")
    timeline.self_note("second candidate should score better")
    page = timeline.render_html(tmp_path / "timeline.html", submissions=[one, two]
                                ).read_text(encoding="utf-8")
    assert 'href="programs/one/files/main.py"' in page
    assert "dependencies: declared, unverified" in page
    assert "dependencies: verified" in page
    assert "--- one/main.py" in page and "+value = 2" in page
    assert "Agent claim" in page


def test_timeline_reopens_append_only_without_renumbering(tmp_path):
    path = tmp_path / "development.jsonl"
    DevelopmentTimeline(path).append("game", summary="started")
    reopened = DevelopmentTimeline(path)
    reopened.append("game", summary="stopped")
    assert [(r["seq"], r["summary"]) for r in reopened.read()] == [
        (1, "started"), (2, "stopped")]
def test_recovery_append_never_reuses_a_sparse_historical_sequence(tmp_path):
    from coding4game_gui.benchmark.evidence.timeline import DevelopmentTimeline
    path=tmp_path/'timeline.jsonl'
    original=b'{"seq":700,"kind":"old"}\n{"seq":701\n'
    path.write_bytes(original)
    row=DevelopmentTimeline(path).append('recovery')
    assert row['seq']==701
    assert path.read_bytes().startswith(original)

