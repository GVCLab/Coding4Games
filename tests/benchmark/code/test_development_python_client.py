"""Exercise developer imports through staged clients and the loopback bridge."""
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from types import SimpleNamespace


from coding4game_gui.benchmark.code.agent_driver import prepare_agent_run
from coding4game_gui.benchmark.code.tool_bridge import ToolServer
from coding4game_gui.wsl2.supervisor import WslSupervisor


def test_nested_debug_script_uses_staged_client_and_scoped_environment(tmp_path):
    agent = "claude"
    profile = SimpleNamespace(
        python=Path(sys.executable), codex=tmp_path / "codex.exe",
        agent=lambda name: PurePosixPath(f"/opt/test/{name}"), development_tools=None,
        ports={"gateway": 51005})
    supervisor = object.__new__(WslSupervisor)
    supervisor.workspace = tmp_path / "workspace"
    supervisor.runtime = SimpleNamespace(profile=profile)
    supervisor.role = "development"
    supervisor._dependency_preparing = supervisor._dependency_failed = False

    def stage(files, name):
        directory = supervisor.workspace / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            (directory / filename).write_bytes(content)
        return str(directory)

    supervisor.stage = stage
    calls = []

    class Session:
        def dispatch(self, tool, arguments, *, phase):
            calls.append((tool, arguments, phase))
            return {"done": False, "tics": 7}

    gateway = SimpleNamespace(base_url="http://127.0.0.1:51005", port=51005,
                              token="g" * 40, model="test-only")
    with ToolServer(Session()) as bridge:
        prepared = prepare_agent_run(supervisor, profile, gateway, bridge,
                                     "test-only", agent=agent)
        captured = {}
        def launch(argv, *, cwd, env, stdin_text, output_mode):
            assert output_mode == "stream"
            captured.update(env)
            return {"handle": "test-only"}
        supervisor._launch = launch
        supervisor.launch_agent(agent, prepared.argv, gateway=prepared.gateway,
                                cwd=prepared.cwd, connection_env=prepared.connection_env,
                                stdin_text=prepared.stdin_text)
        environment = captured
        project = supervisor.workspace / "project"
        project.mkdir()
        script = project / "debug.py"
        script.write_text(
            "import json\nfrom c4g_client import Client\n"
            "print(json.dumps(Client.from_env().call('game.observe')))\n",
            encoding="utf-8")
        child_env = dict(os.environ, PYTHONPATH="untrusted-host-path")
        child_env.pop("C4G_EPISODE_ID", None)
        child_env.update(environment)
        result = subprocess.run([environment["C4G_PYTHON"], str(script)],
                                cwd=project, env=child_env, capture_output=True,
                                text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"done": False, "tics": 7}
    assert calls == [("game.observe", {}, "development")]
    assert not (project / "c4g_client.py").exists()
    assert environment["PYTHONPATH"] == str(supervisor.workspace / "mcp")
