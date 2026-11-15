from scripts import setup_wsl2_runtime as setup
import hashlib
import io
import json
import subprocess
import socket

import pytest


def test_provisioning_constants_pin_clean_native_claude():
    assert setup.DISTRO == "C4G-WSL2-Base-20261013"
    assert setup.CLAUDE_VERSION == "2.1.274"
    assert len(setup.CLAUDE_SHA256) == 64
    assert "python3-numpy" in setup.PACKAGES
    assert "python3-opencv" in setup.PACKAGES
    assert "sudo" not in setup.PACKAGES


class _BoundSocket:
    def __init__(self, collisions=()):
        self.collisions = set(collisions)
        self.options = []
        self.closed = False
        self.address = None

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, address):
        self.address = address
        if address[1] in self.collisions:
            raise OSError(10048, "already in use")

    def close(self):
        self.closed = True


def _netsh_result(command, **_kwargs):
    text = " ".join(command)
    if "dynamicportrange" in text:
        return subprocess.CompletedProcess(command, 0, stdout=(
            "Protocol tcp Dynamic Port Range\nStart Port      : 1024\n"
            "Number of Ports : 13977\n"))
    return subprocess.CompletedProcess(command, 0, stdout=(
        "Protocol tcp Port Exclusion Ranges\nStart Port    End Port\n"
        "----------    --------\n      5357        5357\n     50000       50059     *\n"))


def test_free_ports_avoid_windows_dynamic_allocator_and_hold_exclusive_sockets(monkeypatch):
    sockets = []
    monkeypatch.setattr(setup.subprocess, "run", _netsh_result)
    monkeypatch.setattr(setup.socket, "socket", lambda *_args, **_kwargs: sockets.append(_BoundSocket()) or sockets[-1])
    ports = setup.free_ports()
    assert len(ports) == len(set(ports)) == 5
    assert all(port > 1024 for port in ports)
    assert all(not 1024 <= port <= 15000 for port in ports)
    assert all(sock.options == [(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)]
               for sock in sockets)
    assert all(sock.closed for sock in sockets)


def test_dynamic_port_parser_rejects_ambiguous_and_invalid_output():
    with pytest.raises(RuntimeError, match="ambiguous"):
        setup.parse_dynamic_port_range("A: 10000\nB: 100\nC: 7\n")
    with pytest.raises(RuntimeError, match="invalid"):
        setup.parse_dynamic_port_range("A: 65000\nB: 1000\n")


def test_excluded_port_parser_accepts_rows_and_rejects_malformed_table():
    assert setup.parse_excluded_port_ranges(
        "Localized heading\nStart End\n----- -----\n 5357 5357\n 50000 50059 *\n") == [
            (5357, 5357), (50000, 50059)]
    with pytest.raises(RuntimeError, match="ambiguous"):
        setup.parse_excluded_port_ranges("Localized heading without a table\n")
    with pytest.raises(RuntimeError, match="ambiguous"):
        setup.parse_excluded_port_ranges("Start End\n----- -----\n50000 ??? 50059\n")


def test_free_ports_skip_forbidden_and_permanent_collision_then_close_all(monkeypatch):
    sockets = []
    monkeypatch.setattr(setup, "windows_forbidden_tcp_ranges", lambda: [(1025, 20000)])
    monkeypatch.setattr(setup.socket, "socket", lambda *_args, **_kwargs:
                        sockets.append(_BoundSocket({20002})) or sockets[-1])
    ports = setup.free_ports(2, forbidden_ranges=[(20001, 20001)])
    assert ports == [20003, 20004]
    assert all(sock.closed for sock in sockets)


def test_free_ports_fails_diagnostically_when_fixed_range_is_exhausted(monkeypatch):
    monkeypatch.setattr(setup, "windows_forbidden_tcp_ranges", lambda: [(1025, 65535)])
    with pytest.raises(RuntimeError, match="cannot find 2 fixed TCP ports"):
        setup.free_ports(2)


def test_create_runtime_profile_reuses_baseline_and_forwards_guest_forbidden_range(
        tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.tar"
    baseline.write_bytes(b"immutable")
    seen = {}
    monkeypatch.setattr(setup, "sha256", lambda _: "a" * 64)
    monkeypatch.setattr(setup.secrets, "token_hex", lambda _: "newprofile")

    def fake_free_ports(*, forbidden_ranges):
        seen["forbidden_ranges"] = forbidden_ranges
        return [21001, 21002, 21003, 21004, 21005]

    monkeypatch.setattr(setup, "free_ports", fake_free_ports)
    path = setup.create_runtime_profile(
        tmp_path / "profiles", baseline, forbidden_ranges=[(44620, 48715)])
    profile = json.loads(path.read_text())
    assert seen["forbidden_ranges"] == [(44620, 48715)]
    assert profile["baseline"] == baseline.resolve().as_posix()
    assert profile["ports"]["gateway"] == 21005
    assert baseline.read_bytes() == b"immutable"


def test_binary_transfer_uses_stdin_and_no_host_mount():
    command = setup.claude_install_command("Fresh-Distro")
    assert command[-2:] == ["/dev/stdin", "/opt/c4g-tools/claude"]
    assert command[1:3] == ["-d", "Fresh-Distro"]
    assert not any("/mnt/" in part or "C4G/wsl2" in part for part in command)


def test_guest_setup_script_uses_stdin_not_command_line(monkeypatch):
    call = {}

    def fake_run(args, **kwargs):
        call["args"] = args
        call["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    setup.run_guest_script("Fresh-Distro", "set -eu\r\nprintf 'safe'\r\n")
    assert call["args"][-2:] == ["bash", "-s"]
    assert call["kwargs"]["input"] == b"set -eu\nprintf 'safe'\n"
    assert b"\r" not in call["kwargs"]["input"]
    assert "text" not in call["kwargs"]
    assert "printf" not in call["args"]


def test_package_manifest_parser_reads_exported_status(tmp_path):
    import tarfile
    status = "".join(
        f"Package: {name}\nStatus: install ok installed\nVersion: 1.test\n\n"
        for name in setup.PACKAGES.split())
    archive = tmp_path / "baseline.tar"
    with tarfile.open(archive, "w") as tar:
        data = status.encode(); info = tarfile.TarInfo("var/lib/dpkg/status"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    versions = setup.package_versions_from_tar(archive)
    assert versions["python3"] == "1.test"
    assert set(versions) == set(setup.PACKAGES.split())


@pytest.mark.parametrize("first_run", [False, True])
def test_custom_profile_root_owns_runtime_directory(tmp_path, monkeypatch, first_run):
    from coding4game_gui.operator_config import LAUNCHER_DEFAULTS, load_preset, save_preset

    root = tmp_path / "baseline"
    profile_root = tmp_path / "custom-profiles"
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    preset = tmp_path / "launcher.local.json"
    settings = dict(LAUNCHER_DEFAULTS, model="custom-model", token_limit=9000)
    save_preset(preset, settings)
    monkeypatch.setattr(setup, "REPO", tmp_path)
    monkeypatch.setattr(setup, "CLAUDE_SHA256", hashlib.sha256(b"pinned").hexdigest())
    downloads = []
    monkeypatch.setattr(setup, "urlopen", lambda url, **_: downloads.append(url) or io.BytesIO(b"pinned"))
    monkeypatch.setattr(setup, "distros", lambda: set())
    monkeypatch.setattr(setup, "stream_claude", lambda *_: None)
    monkeypatch.setattr(setup, "free_ports", lambda: [11001, 11002, 11003, 11004, 11005])
    monkeypatch.setattr(setup.secrets, "token_hex", lambda _: "fixed123")
    monkeypatch.setattr(setup, "native_python_dependencies", lambda _: {
        "cv2": "4.6.0", "numpy": "1.24.2", "PIL": "9.4.0", "requests": "2.28.1"})
    monkeypatch.setattr(setup, "write_manifest", lambda *_: root / "baseline-manifest.json")

    commands = []

    def fake_run(*args, **kwargs):
        commands.append(args)
        if args[1:2] == ("--export",):
            (root / "baseline.tar").write_bytes(b"baseline")
        return subprocess.CompletedProcess(args, 0)

    metadata = '{"uid":1000,"claude_sha256":"hash","claude_version":"2.1.274",' \
               '"python":"Python 3.11"}'
    monkeypatch.setattr(setup, "run", fake_run)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout=metadata))

    source = ["--first-run"] if first_run else ["--claude-source", str(claude)]
    assert setup.main(["--root", str(root), "--profile-root", str(profile_root),
                       *source, "--execute"]) == 0
    profile_path = profile_root / "profile-fixed123.json"
    profile = json.loads(profile_path.read_text())
    assert profile["root"] == (profile_root / "runtime-fixed123").resolve().as_posix()
    if first_run:
        assert downloads == [setup.CLAUDE_URL]
        assert commands[0][-2:] == ("--version", "2")
        assert load_preset(preset) == dict(settings, profile="custom-profiles/profile-fixed123.json")
        assert setup.main(["--first-run", "--execute"]) == 0
        assert len(downloads) == 1
        assert len([command for command in commands if "--install" in command]) == 1
    else:
        assert downloads == []
        assert load_preset(preset) == settings


def test_first_run_preview_and_bad_download_do_not_provision(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "REPO", tmp_path)
    downloads = []
    monkeypatch.setattr(setup, "urlopen", lambda url, **_: downloads.append(url) or io.BytesIO(b"corrupt"))
    monkeypatch.setattr(setup, "distros", lambda: pytest.fail("WSL was queried before verification"))
    monkeypatch.setattr(setup, "run", lambda *_: pytest.fail("WSL setup was started"))

    assert setup.main(["--first-run"]) == 0
    assert json.loads(capsys.readouterr().out)["claude_sha256"] == setup.CLAUDE_SHA256
    assert downloads == []
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(RuntimeError, match="Downloaded Claude binary hash mismatch"):
        setup.main(["--first-run", "--execute"])
    assert downloads == [setup.CLAUDE_URL]
    runtime = tmp_path / "runtime" / "wsl2"
    assert list((runtime / "downloads").iterdir()) == []
    assert not (runtime / "baseline").exists()
    assert not (tmp_path / "launcher.local.json").exists()


def test_missing_mandatory_cv2_prevents_export(tmp_path, monkeypatch):
    root = tmp_path / "baseline"
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    commands = []
    monkeypatch.setattr(setup, "sha256", lambda path: setup.CLAUDE_SHA256)
    monkeypatch.setattr(setup, "distros", lambda: set())
    monkeypatch.setattr(setup, "stream_claude", lambda *_: None)

    def fake_run(*args, **kwargs):
        commands.append(args)
        if args[1:2] == ("--export",):
            (root / "baseline.tar").write_bytes(b"baseline")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(setup, "run", fake_run)
    monkeypatch.setattr(setup, "write_manifest", lambda *_: root / "baseline-manifest.json")

    def fake_subprocess_run(command, **kwargs):
        if setup.NATIVE_PYTHON in command:
            assert "cv2" in command[-1]
            raise subprocess.CalledProcessError(
                1, command, stderr="ModuleNotFoundError: No module named 'cv2'")
        return subprocess.CompletedProcess(command, 0, stdout=(
            '{"uid":1000,"claude_sha256":"hash","claude_version":"2.1.274",'
            '"python":"Python 3.11"}'))

    monkeypatch.setattr(setup.subprocess, "run", fake_subprocess_run)

    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero exit status"):
        setup.main(["--root", str(root), "--profile-root", str(tmp_path / "profiles"),
                    "--claude-source", str(claude), "--execute"])
    assert not any(len(command) > 1 and command[1] == "--export" for command in commands)
    stages = json.loads((root / setup.SETUP_MARKER).read_text())["stages"]
    assert [event["stage"] for event in stages] == [
        "install_started", "install_completed", "failed"]
    assert "ModuleNotFoundError" in stages[-1]["error"]


def test_existing_distro_rejected_without_explicit_resume(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    monkeypatch.setattr(setup, "sha256", lambda _: setup.CLAUDE_SHA256)
    monkeypatch.setattr(setup, "distros", lambda: {setup.DISTRO})
    with pytest.raises(RuntimeError, match="without explicit setup resume"):
        setup.main(["--root", str(tmp_path / "baseline"),
                    "--profile-root", str(tmp_path / "profiles"),
                    "--claude-source", str(claude), "--execute"])


@pytest.mark.parametrize("field,value", [
    ("distro", "Some-Other-Distro"),
    ("install_path", "C:/wrong/root/distro"),
])
def test_resume_rejects_wrong_ownership_marker(tmp_path, monkeypatch, field, value):
    root = tmp_path / "baseline"
    root.mkdir()
    marker = setup.new_setup_marker(root, setup.DISTRO, root / "baseline.tar")
    record = json.loads(marker.read_text())
    record[field] = value
    marker.write_text(json.dumps(record))
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    monkeypatch.setattr(setup, "sha256", lambda _: setup.CLAUDE_SHA256)
    monkeypatch.setattr(setup, "distros", lambda: {setup.DISTRO})
    with pytest.raises(RuntimeError, match="ownership marker does not match"):
        setup.main(["--root", str(root), "--profile-root", str(tmp_path / "profiles"),
                    "--claude-source", str(claude), "--execute", "--resume-setup"])


def test_resume_rejects_wrong_registered_distro_root(tmp_path, monkeypatch):
    root = tmp_path / "baseline"
    root.mkdir()
    setup.new_setup_marker(root, setup.DISTRO, root / "baseline.tar")
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    monkeypatch.setattr(setup, "sha256", lambda _: setup.CLAUDE_SHA256)
    monkeypatch.setattr(setup, "distros", lambda: {setup.DISTRO})
    monkeypatch.setattr(setup, "distro_base_path", lambda _: tmp_path / "wrong-distro")
    with pytest.raises(RuntimeError, match="BasePath does not match"):
        setup.main(["--root", str(root), "--profile-root", str(tmp_path / "profiles"),
                    "--claude-source", str(claude), "--execute", "--resume-setup"])


def test_valid_resume_revalidates_and_exports_without_install(tmp_path, monkeypatch):
    root = tmp_path / "baseline"
    root.mkdir()
    profile_root = tmp_path / "profiles"
    setup.new_setup_marker(root, setup.DISTRO, root / "baseline.tar")
    claude = tmp_path / "claude"
    claude.write_bytes(b"pinned")
    commands = []
    guest_scripts = []
    monkeypatch.setattr(setup, "sha256", lambda _: setup.CLAUDE_SHA256)
    monkeypatch.setattr(setup, "distros", lambda: {setup.DISTRO})
    monkeypatch.setattr(setup, "distro_base_path", lambda _: root / "distro")
    monkeypatch.setattr(setup, "stream_claude", lambda *_: None)
    monkeypatch.setattr(setup, "native_python_dependencies", lambda _: {
        "cv2": "4.10.0", "numpy": "2.2.4", "PIL": "11.1.0", "requests": "2.32.3"})
    monkeypatch.setattr(setup, "free_ports", lambda: [11001, 11002, 11003, 11004, 11005])
    monkeypatch.setattr(setup.secrets, "token_hex", lambda _: "resumed123")
    monkeypatch.setattr(setup, "write_manifest", lambda *_: root / "baseline-manifest.json")
    monkeypatch.setattr(setup, "run_guest_script", lambda distro, script: guest_scripts.append(script))

    def fake_run(*args, **kwargs):
        commands.append(args)
        if args[1:2] == ("--export",):
            (root / "baseline.tar").write_bytes(b"baseline")
        return subprocess.CompletedProcess(args, 0)

    metadata = ('{"uid":1000,"claude_sha256":"hash","claude_version":"2.1.274",'
                '"python":"Python 3.13"}')
    monkeypatch.setattr(setup, "run", fake_run)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, stdout=metadata))

    assert setup.main(["--root", str(root), "--profile-root", str(profile_root),
                       "--claude-source", str(claude), "--execute", "--resume-setup"]) == 0
    assert guest_scripts and "/root/.claude" in guest_scripts[0]
    assert "actual_uid" in guest_scripts[0]
    assert not any("--install" in command for command in commands)
    assert any(len(command) > 1 and command[1] == "--export" for command in commands)
    stages = json.loads((root / setup.SETUP_MARKER).read_text())["stages"]
    assert [event["stage"] for event in stages] == ["resume_started", "completed"]
