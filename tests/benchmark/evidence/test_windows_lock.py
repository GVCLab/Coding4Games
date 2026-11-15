"""Cross-platform lock shim: exclusive, position-preserving, no lost lines."""
import json
import threading

from coding4game_gui.benchmark.evidence.metrics import append_jsonl


def test_concurrent_appends_lose_no_lines(tmp_path):
    p = tmp_path / "rows.jsonl"

    def worker(k):
        for i in range(25):
            append_jsonl(p, {"w": k, "i": i})

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    lines = [json.loads(l) for l in p.read_text().splitlines()]
    assert len(lines) == 100
    for k in range(4):
        assert sorted(r["i"] for r in lines if r["w"] == k) == list(range(25))
