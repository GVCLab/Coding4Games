"""Codex runtime leases, Linux paths and final-test denial remain host-owned."""
import json
import hashlib
import io
import tarfile
import tomllib
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from coding4game_gui.wsl2.profile import WslProfile
from coding4game_gui.wsl2.supervisor import WslSupervisor
from coding4game_gui.benchmark.code.codex_driver import prepare_codex_run
from coding4game_gui.benchmark.code.run_codex import _context_compaction_plan, _agent_runtime
from scripts.setup_wsl2_runtime import codex_package_files


def test_profile_accepts_codex_without_requiring_claude(tmp_path):
    value = {"format": "c4g-wsl2-v1", "id": "0123456789ab",
             "root": str(tmp_path / "runtime-0123456789ab"), "baseline": str(tmp_path / "baseline.tar"),
             "baseline_sha256": "a" * 64, "user": "c4gprobe", "uid": 1000,
             "python": "/usr/bin/python3", "agents": {"codex": "/opt/c4g-tools/codex-runtime/bin/codex"},
             "ports": dict(zip(("development_control", "development_game", "final_test_control",
                                "final_test_game", "gateway"), range(15001, 15006)))}
    profile = WslProfile(value)
    assert profile.codex == PurePosixPath(value["agents"]["codex"])
    assert _agent_runtime(profile, "codex")["protocol"] == "responses"
    assert _agent_runtime(profile, "codex")["tool_wait"]["default_exec_yield_time_ms"]==3600000
    with pytest.raises(ValueError, match="not configured"):
        profile.agent("claude")


def supervisor(role="development"):
    result = object.__new__(WslSupervisor)
    result.role = role
    result._dependency_preparing = result._dependency_failed = False
    result._final_execution_started = False
    result.workspace = PurePosixPath("/home/c4gprobe/run/session")
    result.runtime = SimpleNamespace(profile=SimpleNamespace(
        python=PurePosixPath("/usr/bin/python3"), ports={"gateway": 15005},
        agent=lambda name: PurePosixPath("/opt/c4g-tools/codex-runtime/bin/codex")))
    return result


def test_codex_launch_receives_only_scoped_credentials_and_private_home(monkeypatch):
    result = supervisor()
    captured = {}
    monkeypatch.setattr(result, "_launch", lambda argv, **kw: captured.update(argv=argv, **kw) or {"handle": "test"})
    lease = {"url": "http://127.0.0.1:15005/v1", "token": "scoped-test-only-" * 4, "port": 15005}
    result.launch_codex([str(result.runtime.profile.agent("codex")), "--version"], gateway=lease)
    assert captured["env"]["C4G_GATEWAY_TOKEN"] == lease["token"]
    assert captured["env"]["CODEX_HOME"] == "/home/c4gprobe/run/session/.codex"
    assert captured["output_mode"] == "stream"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]
    with pytest.raises(ValueError, match="configured Linux"):
        result.launch_codex(["/bin/sh"], gateway=lease)
    with pytest.raises(ValueError, match="unsupported connection environment"):
        result.launch_codex([str(result.runtime.profile.agent("codex"))], gateway=lease,
                            connection_env={"C4G_GATEWAY_TOKEN": "foreign"})


def test_final_test_rejects_codex_launch_and_model_lease_before_dispatch(monkeypatch):
    result = supervisor("final_test")
    monkeypatch.setattr(result, "call", lambda *a, **kw: pytest.fail("RPC must not run"))
    with pytest.raises(ValueError, match="development-only"):
        result.launch_codex(["/opt/c4g-tools/codex-runtime/bin/codex"], gateway={})
    for env in ({"C4G_GATEWAY_TOKEN": "test"}, {"CODEX_HOME": "/tmp/home"}):
        with pytest.raises(ValueError, match="forbidden"):
            result.launch(["/usr/bin/python3"], env=env)


def test_codex_config_and_mcp_paths_are_linux_and_contain_no_literal_lease():
    staged = {}
    result = supervisor()
    result.stage = lambda files, name: staged.update({name: files}) or result.workspace / name
    profile = SimpleNamespace(python=PurePosixPath("/usr/bin/python3"),
                              codex=result.runtime.profile.agent("codex"))
    gateway = SimpleNamespace(model="test-only", base_url="http://127.0.0.1:15005/v1",
                              token="scoped-test-only", port=15005)
    bridge = SimpleNamespace(url="http://127.0.0.1:15002/call", token="bridge-test-only")
    prepared = prepare_codex_run(result, profile, gateway, bridge, "test-only")
    config = staged[".codex"]["config.toml"].decode()
    assert 'command = "/usr/bin/python3"' in config
    assert "/home/c4gprobe/run/session/mcp/client.py" in config
    assert 'env_key = "C4G_GATEWAY_TOKEN"' in config
    assert gateway.token not in config and bridge.token not in config
    assert prepared["argv"][0] == str(profile.codex)
    assert prepared["stdin_text"] == "test-only"


def test_codex_long_mcp_calls_wait_without_code_mode_polling():
    from coding4game_gui.benchmark.code.codex_driver import _configuration
    profile=SimpleNamespace(python='/usr/bin/python3')
    gateway=SimpleNamespace(model='gpt-5.6-sol',base_url='http://127.0.0.1:15005/v1')
    config=tomllib.loads(_configuration(profile,gateway,'/workspace/mcp/client.py').decode())
    assert config['features']['code_mode_host'] is True
    assert config['features']['code_mode']['enabled'] is True
    assert config['features']['code_mode']['default_exec_yield_time_ms']==3600000
    assert config['mcp_servers']['c4g_game']['tool_timeout_sec']==3600
    assert config['model_providers']['c4g_gateway']['env_key']=='C4G_GATEWAY_TOKEN'


def test_codex_compaction_preserves_strict_accounting_boundary():
    assert _context_compaction_plan("codex", "usage")["mechanism"] == "codex_cli_model_catalog_auto"
    strict = _context_compaction_plan("codex", "strict")
    assert strict["enabled"] is False
    assert strict["disabled_reason"] == "strict_accounting_cannot_reserve_compaction_output"


def test_platform_package_rejects_wrong_hash_before_tar_parsing(tmp_path):
    package = tmp_path / "not-a-package.tgz"
    package.write_bytes(b"not-a-package")
    with pytest.raises(ValueError, match="hash mismatch"):
        codex_package_files(package, "a" * 64)


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "a/../../escape", "a\\escape"])
def test_digest_pinned_package_cannot_escape_runtime_root(tmp_path, unsafe):
    package = tmp_path / "platform.tgz"
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo("package/vendor/x86_64-unknown-linux-musl/" + unsafe)
        info.size = 4
        archive.addfile(info, io.BytesIO(b"test"))
    with pytest.raises(ValueError, match="unsafe"):
        codex_package_files(package, hashlib.sha256(package.read_bytes()).hexdigest())


def test_digest_pinned_package_rejects_symlink_and_duplicate_files(tmp_path):
    for kind in ("symlink", "duplicate"):
        package = tmp_path / (kind + ".tgz")
        with tarfile.open(package, "w:gz") as archive:
            info = tarfile.TarInfo("package/vendor/x86_64-unknown-linux-musl/bin/codex")
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/bin/sh"
                archive.addfile(info)
            else:
                info.size = 4
                for _ in range(2):
                    archive.addfile(info, io.BytesIO(b"\x7fELF"))
        with pytest.raises(ValueError):
            codex_package_files(package, hashlib.sha256(package.read_bytes()).hexdigest())
