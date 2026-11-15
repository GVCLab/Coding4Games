"""Layout checks without starting WSL, game processes or paid inference."""
import ast
import subprocess
import sys

import pytest

from coding4game_gui.paths import REPO_ROOT, SCENARIO_DIR, runtime_source, development_template
from coding4game_gui.benchmark import __main__ as cli
from coding4game_gui.benchmark.experiment_plan import discover_benchmark_scenarios
from coding4game_gui.catalog import FORMAL_SCENARIOS


def test_registered_scenarios_and_prompt_are_packaged_resources():
    registered = discover_benchmark_scenarios()
    assert {item.slug for item in registered} == set(FORMAL_SCENARIOS)
    assert all(item.path.parent == SCENARIO_DIR for item in registered)
    assert 'submission.finish' in development_template()
    with pytest.raises(ValueError):
        runtime_source('../auth.json')


def test_standalone_runtime_resources_import_without_host_package(tmp_path):
    for name in ('wire.py', 'bundle.py', 'client.py'):
        source = runtime_source(name)
        (tmp_path / name).write_bytes(source)
        tree = ast.parse(source)
        assert not any(isinstance(n, ast.ImportFrom) and
                       (n.module or '').startswith('coding4game_gui') for n in ast.walk(tree))
    command = "import runpy; c=runpy.run_path('client.py'); assert 'Client' in c"
    result = subprocess.run([sys.executable, '-I', '-B', '-c', command],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('phase', ['develop', 'test'])
def test_unified_program_cli_dispatch_preserves_arguments(monkeypatch, phase):
    from coding4game_gui.benchmark.code import run_codex
    monkeypatch.setattr(run_codex, 'main', lambda args: args)
    assert cli.main([phase, '--help']) == [phase, '--help']


@pytest.mark.parametrize('phase', ['develop', 'test', 'direct'])
def test_public_help_runs_from_repo_without_effects(phase):
    result = subprocess.run([sys.executable, '-B', '-m', 'coding4game_gui.benchmark', phase, '--help'],
                            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert '--scenario' in result.stdout
