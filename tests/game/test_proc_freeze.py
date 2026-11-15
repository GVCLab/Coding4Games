"""Verify the OS process-freeze primitive actually halts a process.

This backs the "fully pause while the agent thinks" behavior (env.pause() →
NtSuspendProcess). Windows-only; skips elsewhere.

Run:  python -m pytest tests/game/test_proc_freeze.py -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if sys.platform != "win32":
    pytest.skip("process freeze is Windows-only", allow_module_level=True)

from coding4game_gui.benchmark.windows.drivers.windows_xinput import proc_freeze


def test_suspend_truly_freezes_then_resume_unfreezes():
    assert proc_freeze.available()
    f = os.path.join(tempfile.gettempdir(), "c4g_freeze_test.txt")
    open(f, "w").close()
    child = (
        "import time\n"
        f"fn=r'{f}'\n"
        "i=0\n"
        "while True:\n"
        "    i+=1; open(fn,'a').write(str(i)+chr(10)); time.sleep(0.05)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", child])
    try:
        time.sleep(0.5)
        before = sum(1 for _ in open(f))
        assert proc_freeze.suspend_process(p.pid) is True
        time.sleep(0.7)
        during = sum(1 for _ in open(f))
        assert proc_freeze.resume_process(p.pid) is True
        time.sleep(0.5)
        after = sum(1 for _ in open(f))
    finally:
        p.terminate()
        p.wait(timeout=5)

    # Frozen while suspended: at most 1 in-flight tick may land.
    assert during - before <= 1, f"process kept running while suspended (+{during - before})"
    # Alive again after resume.
    assert after - during >= 3, f"process did not resume (+{after - during})"
