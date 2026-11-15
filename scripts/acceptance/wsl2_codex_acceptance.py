"""Real Linux Codex and runtime acceptance with synthetic responses/game only.

Never contacts a model provider or starts a Windows game. This probe verifies
CLI/MCP/shell transport, immutable submission, and a separately invoked no-agent
final test. It does not establish model quality or real-game acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class FixtureEnv:
    TICRATE = 10

    def __init__(self):
        self.tic = 0
        self.total_reward = 0.0
        self.done = False
        self.end_reason = None

    def reset(self, *args): pass

    def close(self): pass
    def validate_action_plan(self, *args): pass
    def frame(self):
        import numpy as np
        return np.zeros((8, 8, 3), dtype=np.uint8)
    def window_info(self): return {"client_width": 8, "client_height": 8}
    def act(self, controls, tics):
        if controls != ["NEUTRAL"]:
            raise ValueError("fixture accepts neutral input only")
        self.tic += tics


PROGRAM = '''from c4g_client import Client
result = Client.from_env().call("game.step", controls=["NEUTRAL"], tics=1)
assert result["tics"] == 1
print("FROZEN_FIXTURE_PROGRAM_OK")
'''


def workflow_command():
    code = f'''import json
from pathlib import Path
from c4g_client import Client
p=Path("fixture_program"); p.mkdir()
(p/"main.py").write_text({PROGRAM!r})
c=Client.from_env()
record=c.call("submission.save",path="fixture_program",entrypoint={{"argv":["main.py"],"mode":"stepped","runtime":"python"}})
evaluated=c.call("submission.run",id=record["id"],max_tics=3)
assert evaluated["program_returncode"] == 0, evaluated
selected=c.call("submission.finish",id=record["id"])
assert selected["id"] == record["id"]
print("EXPLICIT_FIXTURE_SELECTION_OK")
'''
    return '"$C4G_PYTHON" - <<\'C4G_FIXTURE\'\n' + code + "C4G_FIXTURE\n"


def sse(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


class SyntheticResponse:
    status = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, item):
        response = {"id": "resp_fixture_" + uuid.uuid4().hex, "object": "response",
                    "created_at": 1, "status": "in_progress", "output": []}
        self.body = sse("response.created", response=response)
        initial = {**item, "status": "in_progress"}
        if item["type"] == "custom_tool_call":
            initial["input"] = ""
        self.body += sse("response.output_item.added", output_index=0, item=initial)
        if item["type"] == "custom_tool_call":
            self.body += sse("response.custom_tool_call_input.delta", output_index=0,
                             item_id=item["id"], delta=item["input"])
            self.body += sse("response.custom_tool_call_input.done", output_index=0,
                             item_id=item["id"], input=item["input"])
        self.body += sse("response.output_item.done", output_index=0, item=item)
        self.body += sse("response.completed", response={
            **response, "status": "completed", "output": [item],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}})

    def iter_bytes(self): yield self.body
    def close(self): pass


def function_tools(tools, prefix=""):
    found = []
    for tool in tools:
        if tool.get("type") == "namespace":
            found.extend(function_tools(tool.get("tools", []), prefix + tool["name"] + "."))
        elif tool.get("type") in {"function", "custom"}:
            found.append((prefix + tool["name"], tool))
    return found


def execute(profile, out):
    from coding4game_gui.benchmark.code import run_codex as runner
    from coding4game_gui.benchmark.code.model_gateway import ModelGateway
    from coding4game_gui.wsl2.profile import WslProfile
    from coding4game_gui.wsl2.native import WslNative
    WslProfile.load(profile).agent("codex")
    out = out.resolve()
    if out.exists():
        raise ValueError("probe evidence directory must be new")
    out.mkdir(parents=True)
    calls = []
    requests = 0
    report = {"status": "running", "kind": "real_cli_synthetic_transport_and_game",
              "real_coding_cli": True, "paid_model_calls": 0, "real_game_started": False,
              "autonomous_benchmark_eligible": False}

    def transport(method, url, headers, body, timeout):
        nonlocal requests
        requests += 1
        if requests > 8:
            raise RuntimeError("synthetic fixture request limit reached")
        payload = json.loads(body)
        advertised = list(payload.get("tools", []))
        for item in payload.get("input", []):
            if item.get("type") == "additional_tools":
                advertised.extend(item.get("tools", []))
        tools = function_tools(advertised)
        names = [name for name, _ in tools]
        (out / "advertised_tools.json").write_text(json.dumps(names, indent=2))
        code_mode = next((name for name, tool in tools if name.endswith(".exec") and
                          tool.get("type") == "custom"), None)
        if code_mode:
            code = (
                "const t = ALL_TOOLS.find(t => t.name.includes('c4g_game') && "
                "(t.name.endsWith('bench_status') || t.name.endsWith('bench_status_get'))); "
                "if (!t) throw new Error('game MCP status tool missing'); "
                "text(await tools[t.name]({}));"
                if not calls else
                "text(await tools.exec_command(" + json.dumps({
                    "cmd": workflow_command(), "yield_time_ms": 10000,
                    "max_output_tokens": 2000}) + "));"
            )
            if len(calls) >= 2:
                raise RuntimeError("code-mode workflow did not select a program")
            namespace, name = code_mode.rsplit(".", 1)
            calls.append({"index": len(calls) + 1, "tool": code_mode,
                          "action": "mcp_status" if not calls else "shell_submission"})
            return SyntheticResponse({"id": "ctc_" + uuid.uuid4().hex,
                "type": "custom_tool_call", "status": "completed", "namespace": namespace,
                "call_id": "call_" + uuid.uuid4().hex, "name": name, "input": code})
        status_tool = next((name for name in names if "c4g_game" in name and
                            (name.endswith("bench_status") or name.endswith("bench.status"))), None)
        shell = next((name for name in names if name.rsplit(".", 1)[-1] == "exec_command"), None)
        if not calls and status_tool:
            name, arguments = status_tool, {}
        elif shell and len(calls) < 2:
            name, arguments = shell, {"cmd": workflow_command(), "yield_time_ms": 10000,
                                      "max_output_tokens": 2000}
        else:
            raise RuntimeError("required MCP/shell tools missing or fixture workflow did not select a program")
        calls.append({"index": len(calls) + 1, "tool": name})
        return SyntheticResponse({"id": "fc_" + uuid.uuid4().hex, "type": "function_call",
            "status": "completed", "call_id": "call_" + uuid.uuid4().hex,
            "name": name, "arguments": json.dumps(arguments)})

    base = ["--profile", str(profile), "--scenario", "balatro", "--windows-input", "gamepad",
            "--tic-budget", "10", "--max-tics", "3", "--wall-seconds", "240", "--execute"]
    deps = runner._dependencies()
    deps.pop("benchmark_settings", None)
    deps["make_bench_env"] = lambda *a, **kw: FixtureEnv()
    deps["load_game_spec"] = lambda *a, **kw: {
        "title": "Synthetic transport acceptance fixture", "engine": "windows",
        "gameplay": "Synthetic fixture; no Windows game is running.",
        "objective": "Evaluate and explicitly select the fixture Python program.",
        "episode_contract": "Diagnostic-only fixture, excluded from game acceptance.",
        "runtime": {"max_tics": 3, "tics_per_second": 10},
        "lifecycle": {"terminal_detection": False},
        "action_catalog": [{"name": "NEUTRAL", "description": "Fixture neutral action"}]}
    deps["ModelGateway"] = lambda *a, **kw: ModelGateway(
        *a, **kw, transport=transport, max_attempts=1)
    previous = os.environ.get("C4G_SYNTHETIC_CODEX_KEY")
    os.environ["C4G_SYNTHETIC_CODEX_KEY"] = "synthetic-not-an-upstream-key"
    try:
        args = runner.parser().parse_args(["develop", *base, "--agent", "codex",
            "--out-dir", str(out / "develop"), "--model", "gpt-5.6-sol",
            "--upstream-url", "https://synthetic.invalid/v1", "--api-key-env", "C4G_SYNTHETIC_CODEX_KEY",
            "--model-proxy-mode", "direct", "--token-limit", "1000", "--confirm-paid-run"])
        development = runner.execute(args, deps=deps)
        report["development"] = development
        if development["status"] != "complete" or not development.get("final_program_id"):
            raise RuntimeError("real Codex did not select an evaluated frozen program")
        final_deps = dict(deps)
        original_session = final_deps["OpenSession"]

        class CheckedFinalSession(original_session):
            def run_program(self, *a, **kw):
                result = super().run_program(*a, **kw)
                paths = [str(path) for path in WslProfile.load(profile).agents.values()]
                code = ("import json,os,subprocess\npaths=" + repr(paths) + "\n"
                        "for p in paths:\n"
                        " assert not os.access(p,os.R_OK) and not os.access(p,os.X_OK)\n"
                        " try: subprocess.run([p,'--version'],check=False,timeout=2)\n"
                        " except PermissionError: pass\n"
                        " else: raise AssertionError('agent executable was accessible')\n"
                        "assert not any(k in os.environ for k in "
                        "('C4G_GATEWAY_TOKEN','OPENAI_API_KEY','CODEX_HOME','ANTHROPIC_API_KEY'))\n"
                        "print(json.dumps({'agent_tools_unreadable_and_unexecutable':True,"
                        "'agent_version_execution_denied':True,'model_environment_absent':True}))")
                child = self.supervisor.launch([self.python, "-I", "-B", "-c", code])
                deadline = min(self.deadline, time.monotonic() + 10)
                output = []
                while time.monotonic() < deadline:
                    state = self.supervisor.poll(child["handle"])
                    output += [event.get("data", "") for event in state.get("events", [])
                               if event.get("stream") == "stdout"]
                    if not state.get("running") and state.get("output_complete", True):
                        if state.get("exit_code") != 0:
                            raise RuntimeError("final Codex denial probe failed")
                        report["final_denial_probe"] = json.loads("".join(output))
                        return result
                    time.sleep(.05)
                self.supervisor.terminate(child["handle"])
                raise TimeoutError("final Codex denial probe timed out")

        final_deps["OpenSession"] = CheckedFinalSession
        final_deps["ModelGateway"] = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("final test attempted model access"))
        final_deps["run_agent"] = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("final test attempted to start a coding agent"))
        source = runner._out_dir(args) / "programs"
        final_args = runner.parser().parse_args(["test", *base, "--out-dir", str(out / "final"),
            "--source-programs", str(source), "--program-id", development["final_program_id"]])
        final = runner.execute(final_args, deps=final_deps)
        report["final_test"] = final
        if final["status"] != "complete" or final["result"].get("program_returncode") != 0:
            raise RuntimeError("independent fixture final test failed")
        registered = WslNative().list_distros()
        clones = [config["runtime_role"]["distro"] for config in (development, final)]
        report["clones_unregistered"] = all(name not in registered for name in clones)
        if not report["clones_unregistered"]:
            raise RuntimeError("probe clone remains registered after cleanup")
        report["status"] = "passed_for_real_cli_and_synthetic_fixture_only"
        return report
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if previous is None:
            os.environ.pop("C4G_SYNTHETIC_CODEX_KEY", None)
        else:
            os.environ["C4G_SYNTHETIC_CODEX_KEY"] = previous
        report["synthetic_requests"] = calls
        (out / "result.json").write_text(json.dumps(report, indent=2, default=str) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"execute": False, "profile": str(args.profile), "out": str(args.out),
                          "paid_model_calls": 0, "real_game_started": False}))
        return 0
    result = execute(args.profile, args.out)
    print(json.dumps({"status": result["status"], "evidence": str(args.out / "result.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
