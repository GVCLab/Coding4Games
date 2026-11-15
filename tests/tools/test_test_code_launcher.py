"""Exercise result discovery without launching a game or a model."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('receipt, succeeds, repeats_written', [
    ({'status': 'complete', 'result_valid': True,
      'result': {'program_returncode': 0}}, True, 3),
    ({'status': 'complete', 'result_valid': True,
      'result': {'program_returncode': 1, 'end_reason': 'tic_budget'}}, False, 1),
    ({'status': 'complete', 'result_valid': False,
      'result': {'program_returncode': 0}}, False, 1),
])
def test_repeats_require_valid_program_execution(tmp_path, receipt, succeeds, repeats_written):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required")
    source = tmp_path / "development"
    program_id = "a" * 64
    (source / "programs" / program_id).mkdir(parents=True)
    (source / "programs" / "final.json").write_text(json.dumps({"id": program_id}))
    profile = tmp_path / "profile.yaml"
    profile.write_text("{}")
    (source / "config.json").write_text(json.dumps({
        "profile": str(profile), "scenario": "black_myth",
        "runtime_backend": "wsl2",
        "windows_input": "gamepad-kbm", "tic_budget": 20000,
    }))
    runner = tmp_path / "fake_runner.ps1"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    runner.write_text('''
if ($args -contains '-c') {
    & $env:C4G_TEST_PYTHON @args
    return
}
if ($args[[array]::IndexOf($args, '--runtime-backend') + 1] -ne 'wsl2' -or
    $args[[array]::IndexOf($args, '--program-id') + 1] -ne ('a' * 64) -or
    $args -contains '--model') { throw 'Unexpected independent-test arguments' }
$outDir = $args[[array]::IndexOf($args, '--out-dir') + 1]
$mode = $args[[array]::IndexOf($args, '--windows-input') + 1]
$suffix = "_input-$mode"
if (-not $outDir.EndsWith($suffix)) { $outDir += $suffix }
$null = New-Item -ItemType Directory -Path $outDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'receipt.json') -Destination (Join-Path $outDir 'config.json')
$global:LASTEXITCODE = 0
''')
    launcher = Path(__file__).resolve().parents[2] / "scripts/launchers/test_code.ps1"
    result = subprocess.run([
        shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
        "-RunDirs", str(source), "-Repeats", "3", "-Python", str(runner), "-Execute",
    ], capture_output=True, text=True, encoding="utf-8", errors="replace",
       env=dict(os.environ, C4G_TEST_PYTHON=sys.executable))
    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    if not succeeds:
        assert 'Test did not complete validly' in result.stdout + result.stderr
    results = list((source / "test").glob("*/repeat_*/config.json"))
    assert len(results) == repeats_written
    assert len({path.parent.parent for path in results}) == 1
