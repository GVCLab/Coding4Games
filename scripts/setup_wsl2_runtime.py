"""Provision a credential-free Debian WSL2 baseline for Coding4Game runs."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import winreg
from http.client import HTTPException
from pathlib import Path
from urllib.request import urlopen


REPO = Path(__file__).resolve().parents[1]
DISTRO = "C4G-WSL2-Base-20261013"
CLAUDE_VERSION = "2.1.274"
CLAUDE_SHA256 = "15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07"
CLAUDE_URL = f"https://downloads.claude.ai/claude-code-releases/{CLAUDE_VERSION}/linux-x64/claude"
CLAUDE_NPM_URL = ("https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/"
                  f"claude-code-linux-x64-{CLAUDE_VERSION}.tgz")
PACKAGES = ("python3 python3-venv python3-pip python3-numpy python3-pil "
            "python3-opencv python3-requests git curl ca-certificates nftables procps nodejs npm "
            "build-essential")
NATIVE_PYTHON = "/usr/bin/python3"
MANDATORY_CLIENT_MODULES = ("cv2", "numpy", "PIL", "requests")
SETUP_MARKER = "pending-setup.json"
SETUP_MARKER_FORMAT = "c4g-wsl2-pending-setup-v1"


def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input_text, text=True, check=True)


def run_guest_script(distro: str, script: str) -> subprocess.CompletedProcess:
    """Send LF-only UTF-8 bytes without Windows text newline conversion."""
    payload = script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return subprocess.run(
        ["wsl.exe", "-d", distro, "-u", "root", "--", "bash", "-s"],
        input=payload, check=True)


def distros() -> set[str]:
    raw = subprocess.run(["wsl.exe", "--list", "--quiet"], capture_output=True,
                         check=True).stdout
    out = raw.decode("utf-16-le") if b"\x00" in raw else raw.decode("utf-8")
    return {line.strip().strip("\x00") for line in out.splitlines() if line.strip(" \x00")}


def canonical_windows_path(path: Path | str) -> str:
    value = str(Path(path).resolve())
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def distro_base_path(distro: str) -> Path:
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as root:
        index = 0
        while True:
            try:
                child_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            with winreg.OpenKey(root, child_name) as child:
                try:
                    name = winreg.QueryValueEx(child, "DistributionName")[0]
                    base_path = winreg.QueryValueEx(child, "BasePath")[0]
                except OSError:
                    continue
                if name == distro:
                    return Path(base_path)
    raise RuntimeError(f"cannot find registry BasePath for distro: {distro}")


def new_setup_marker(root: Path, distro: str, baseline: Path) -> Path:
    marker = root / SETUP_MARKER
    record = {"format": SETUP_MARKER_FORMAT, "distro": distro,
              "install_path": str((root / "distro").resolve()),
              "baseline": str(baseline.resolve()), "claude_sha256": CLAUDE_SHA256,
              "stages": []}
    with marker.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
    return marker


def read_resume_marker(marker: Path, distro: str, root: Path, baseline: Path) -> dict:
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("resume setup ownership marker is missing or invalid") from exc
    expected = {"format": SETUP_MARKER_FORMAT, "distro": distro,
                "install_path": canonical_windows_path(root / "distro"),
                "baseline": canonical_windows_path(baseline),
                "claude_sha256": CLAUDE_SHA256}
    actual = dict(record)
    actual["install_path"] = canonical_windows_path(actual.get("install_path", ""))
    actual["baseline"] = canonical_windows_path(actual.get("baseline", ""))
    if any(actual.get(key) != value for key, value in expected.items()):
        raise RuntimeError("resume setup ownership marker does not match this setup")
    if not isinstance(record.get("stages"), list):
        raise RuntimeError("resume setup ownership marker has invalid stage history")
    return record


def record_setup_stage(marker: Path, stage: str, error: str | None = None) -> None:
    record = json.loads(marker.read_text(encoding="utf-8"))
    event = {"stage": stage, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if error is not None:
        event["error"] = error
    record["stages"].append(event)
    marker.write_text(json.dumps(record, indent=2), encoding="utf-8")


def setup_error_text(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    stderr = getattr(exc, "stderr", None)
    if stderr:
        detail += f"; stderr: {str(stderr).strip()[:2000]}"
    return detail


PortRange = tuple[int, int]


def _netsh(*args: str) -> str:
    try:
        result = subprocess.run(["netsh.exe", "interface", *args], capture_output=True,
                                text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot read Windows TCP port allocation: {' '.join(args)}") from exc
    return result.stdout


def parse_dynamic_port_range(output: str) -> PortRange:
    """Parse the two numeric netsh fields without depending on localized labels."""
    values = []
    for line in output.splitlines():
        if ":" not in line:
            continue
        suffix = line.rsplit(":", 1)[1].strip()
        if re.fullmatch(r"\d+", suffix):
            values.append(int(suffix))
    if len(values) != 2:
        raise RuntimeError("ambiguous Windows dynamic TCP port range output")
    start, count = values
    end = start + count - 1
    if not (1 <= start <= 65535 and count > 0 and end <= 65535):
        raise RuntimeError("invalid Windows dynamic TCP port range")
    return start, end


def parse_excluded_port_ranges(output: str) -> list[PortRange]:
    """Parse numeric netsh table rows, accepting the optional persistent marker."""
    if not re.search(r"-{3,}\s+-{3,}", output):
        raise RuntimeError("ambiguous Windows excluded TCP port range output")
    ranges = []
    in_table = False
    for line in output.splitlines():
        if re.fullmatch(r"\s*-{3,}\s+-{3,}\s*", line):
            in_table = True
            continue
        if not in_table:
            continue
        match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s*\*?\s*", line)
        if match:
            start, end = map(int, match.groups())
            if not (1 <= start <= end <= 65535):
                raise RuntimeError("invalid Windows excluded TCP port range")
            ranges.append((start, end))
        elif re.search(r"\d", line):
            raise RuntimeError("ambiguous Windows excluded TCP port range output")
        else:
            continue
    return ranges


def windows_forbidden_tcp_ranges() -> list[PortRange]:
    ranges = []
    for family in ("ipv4", "ipv6"):
        ranges.append(parse_dynamic_port_range(
            _netsh(family, "show", "dynamicportrange", "protocol=tcp")))
        ranges.extend(parse_excluded_port_ranges(
            _netsh(family, "show", "excludedportrange", "protocol=tcp")))
    return ranges


def free_ports(count: int = 5, *, forbidden_ranges=()) -> list[int]:
    """Choose fixed Windows TCP ports and hold exclusive binds until selection completes."""
    if count < 1:
        raise ValueError("port count must be positive")
    forbidden = windows_forbidden_tcp_ranges()
    for item in forbidden_ranges:
        try:
            start, end = item
        except (TypeError, ValueError) as exc:
            raise ValueError("forbidden port ranges must be (start, end) pairs") from exc
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= 65535):
            raise ValueError("invalid forbidden port range")
        forbidden.append((start, end))

    sockets = []
    ports = []
    collision_codes = {getattr(socket, "EADDRINUSE", 10048), 10013, 10048}
    try:
        for port in range(1025, 65536):
            if any(start <= port <= end for start, end in forbidden):
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                sock.close()
                if exc.errno in collision_codes or getattr(exc, "winerror", None) in collision_codes:
                    continue
                raise RuntimeError(f"cannot test fixed TCP port {port}: {exc}") from exc
            sockets.append(sock)
            ports.append(port)
            if len(ports) == count:
                return ports
        raise RuntimeError(
            f"cannot find {count} fixed TCP ports outside Windows allocation and forbidden ranges")
    finally:
        for sock in sockets:
            sock.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def download_claude_npm(binary: Path) -> None:
    with urlopen(CLAUDE_NPM_URL, timeout=60) as response, tarfile.open(
            fileobj=response, mode="r|gz") as archive:
        for member in archive:
            if member.name != "package/claude":
                continue
            if not member.isreg():
                break
            with archive.extractfile(member) as source, binary.open("wb") as stream:
                shutil.copyfileobj(source, stream)
            return
    raise RuntimeError("Claude npm archive does not contain a regular package/claude file")


def download_claude(directory: Path) -> Path:
    """Cache the pinned Linux executable only after its digest matches."""
    target = directory / f"claude-{CLAUDE_VERSION}-linux-x64"
    if target.is_file() and sha256(target) == CLAUDE_SHA256:
        return target
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Claude Code {CLAUDE_VERSION} for Linux x64.", flush=True)
    with tempfile.TemporaryDirectory(dir=directory) as download_dir:
        binary = Path(download_dir) / "claude"
        try:
            response = urlopen(CLAUDE_URL, timeout=5)
        except (OSError, HTTPException) as error:
            print(f"Claude download unavailable ({type(error).__name__}). "
                  "Trying the pinned Anthropic npm package.", flush=True)
            download_claude_npm(binary)
        else:
            with response, binary.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if sha256(binary) != CLAUDE_SHA256:
            raise RuntimeError("Downloaded Claude binary hash mismatch")
        binary.replace(target)
    return target


def configured_launcher_profile(repo: Path, values: dict) -> Path | None:
    from coding4game_gui.operator_config import PATH_DEFAULTS

    value = values["profile"]
    profile = (repo / value).resolve()
    return profile if value != PATH_DEFAULTS["profile"] or profile.exists() else None


def select_first_run_profile(preset: Path, profile: Path) -> None:
    from coding4game_gui.operator_config import load_preset, save_preset

    values = load_preset(preset)
    repo = preset.parent
    if configured_launcher_profile(repo, values) is not None:
        print(f"Profile created at {profile}; the launcher profile has already been selected.")
        return
    profile = profile.resolve()
    values["profile"] = (profile.relative_to(repo) if profile.is_relative_to(repo) else profile).as_posix()
    with tempfile.TemporaryDirectory(dir=repo) as temporary:
        new_preset = Path(temporary) / preset.name
        save_preset(new_preset, values)
        new_preset.replace(preset)


def create_runtime_profile(profile_root: Path, baseline: Path, *, forbidden_ranges=(), agents=None) -> Path:
    """Create a fresh profile for an existing immutable baseline."""
    if not baseline.is_file():
        raise RuntimeError(f"baseline does not exist: {baseline}")
    profile_id = secrets.token_hex(6)
    names = ("development_control", "development_game", "final_test_control",
             "final_test_game", "gateway")
    selected_ports = (free_ports(forbidden_ranges=forbidden_ranges)
                      if forbidden_ranges else free_ports())
    profile = {"format": "c4g-wsl2-v1", "id": profile_id,
               "root": (profile_root / f"runtime-{profile_id}").resolve().as_posix(),
               "baseline": baseline.resolve().as_posix(),
               "baseline_sha256": sha256(baseline), "user": "c4gprobe", "uid": 1000,
               "python": "/usr/bin/python3",
               "agents": dict(agents) if agents is not None else {"claude": "/opt/c4g-tools/claude"},
               "ports": dict(zip(names, selected_ports))}
    profile_path = profile_root / f"profile-{profile_id}.json"
    profile_root.mkdir(parents=True, exist_ok=True)
    with profile_path.open("x", encoding="utf-8") as stream:
        json.dump(profile, stream, indent=2)
    return profile_path


def claude_install_command(distro: str) -> list[str]:
    return ["wsl.exe", "-d", distro, "-u", "root", "--", "install",
            "-o", "root", "-g", "root", "-m", "0755", "/dev/stdin",
            "/opt/c4g-tools/claude"]


def stream_claude(distro: str, source: Path) -> None:
    """Copy only the pinned executable, without a host mount or user config."""
    process = subprocess.Popen(claude_install_command(distro), stdin=subprocess.PIPE)
    assert process.stdin is not None
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                process.stdin.write(chunk)
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("Claude binary transfer into WSL failed")


def native_python_dependencies(distro: str) -> dict[str, str]:
    """Verify the pinned native interpreter can import every client dependency."""
    imports = ", ".join(MANDATORY_CLIENT_MODULES)
    versions = ", ".join(
        f"{name!r}: getattr({name}, '__version__', 'unknown')"
        for name in MANDATORY_CLIENT_MODULES
    )
    script = f"import json, {imports}; print(json.dumps({{{versions}}}, sort_keys=True))"
    raw = subprocess.run(
        ["wsl.exe", "-d", distro, "-u", "root", "--", NATIVE_PYTHON, "-c", script],
        capture_output=True, text=True, check=True).stdout
    result = json.loads(raw)
    if set(result) != set(MANDATORY_CLIENT_MODULES):
        raise RuntimeError("native Python dependency preflight returned incomplete results")
    return result


def existing_profile(profile_root: Path, baseline: Path) -> tuple[Path, dict] | None:
    for path in profile_root.glob("profile-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if Path(data["baseline"]).resolve() == baseline.resolve():
                return path, data
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def package_versions_from_tar(baseline: Path) -> dict[str, str]:
    wanted = set(PACKAGES.split())
    with tarfile.open(baseline, "r") as archive:
        member = next((m for m in archive if m.name.lstrip("./") == "var/lib/dpkg/status"), None)
        if member is None: raise RuntimeError("baseline lacks dpkg status")
        stream = archive.extractfile(member)
        if stream is None: raise RuntimeError("cannot read baseline dpkg status")
        text = stream.read().decode("utf-8")
    found = {}
    for paragraph in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line)
        if fields.get("Package") in wanted and fields.get("Status") == "install ok installed":
            found[fields["Package"]] = fields["Version"]
    missing = wanted - found.keys()
    if missing: raise RuntimeError(f"baseline lacks requested packages: {sorted(missing)}")
    return dict(sorted(found.items()))


def write_manifest(root: Path, baseline: Path, metadata: Path) -> Path:
    record = {"format": "c4g-wsl2-baseline-manifest-v1",
              "baseline": str(baseline), "baseline_sha256": sha256(baseline),
              "metadata_sha256": sha256(metadata), "claude_version": CLAUDE_VERSION,
              "claude_sha256": CLAUDE_SHA256,
              "packages": package_versions_from_tar(baseline)}
    path = root / "baseline-manifest.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def codex_package_files(package: Path, expected_sha256: str) -> dict[str, bytes]:
    """Read a digest-pinned Linux platform payload without extracting paths."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ValueError("a lowercase Codex package SHA-256 is required")
    if sha256(package) != expected_sha256:
        raise ValueError("Codex package hash mismatch")
    prefix = "package/vendor/x86_64-unknown-linux-musl/"
    files = {}
    total = 0
    with tarfile.open(package, "r:gz") as archive:
        for member in archive:
            if member.name in {"package/package.json", "package/README.md"}:
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise ValueError("unexpected file in the Codex Linux platform package")
            relative = member.name[len(prefix):]
            parts = relative.split("/")
            if (not relative or "\\" in relative or any(part in {"", ".", ".."} for part in parts)
                    or relative in files or len(files) >= 128 or member.size > 1024**3):
                raise ValueError("unsafe or oversized Codex platform payload")
            total += member.size
            if total > 1024**3:
                raise ValueError("Codex platform payload exceeds 1 GiB")
            files[relative] = archive.extractfile(member).read()
    if not files.get("bin/codex", b"").startswith(b"\x7fELF"):
        raise ValueError("Codex package does not contain the expected Linux ELF")
    return files


def extend_codex_baseline(args) -> int:
    """Build a new immutable baseline; never edit the parent distribution or TAR."""
    from coding4game_gui.wsl2.profile import WslProfile
    from coding4game_gui.wsl2.native import WslNative
    profile = WslProfile.load(args.base_profile)
    if not args.codex_package or not args.codex_package_sha256 or not args.codex_version:
        raise ValueError("--base-profile requires --codex-package, --codex-package-sha256 and --codex-version")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.a-zA-Z0-9]*)?", args.codex_version):
        raise ValueError("Codex version must be an exact version")
    root = args.root.resolve()
    baseline = root / "baseline.tar"
    if root.exists():
        raise ValueError("Codex baseline destination must be a new directory")
    if not re.fullmatch(r"C4G-WSL2-Build-[a-zA-Z0-9-]+", args.distro):
        raise ValueError("Codex build distribution must use the C4G-WSL2-Build- prefix")
    plan = {"execute": args.execute, "parent_profile": str(args.base_profile),
            "parent_baseline_sha256": profile.baseline_sha256,
            "codex_version": args.codex_version, "package_sha256": args.codex_package_sha256,
            "distro": args.distro, "root": str(root), "model_calls": 0, "game_started": False}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.execute:
        return 0
    if sha256(profile.baseline) != profile.baseline_sha256:
        raise ValueError("parent baseline hash mismatch")
    files = codex_package_files(args.codex_package, args.codex_package_sha256)
    native = WslNative()
    if args.distro in native.list_distros():
        raise ValueError("Codex build distribution already exists")
    root.mkdir(parents=True)
    marker = root / "codex-setup.json"
    record = {**plan, "status": "preparing", "install_path": str(root / "distro")}
    def save():
        marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    save()
    imported = False
    try:
        native.import_distro(args.distro, root / "distro", profile.baseline)
        imported = True
        native.exec(args.distro, ["/bin/sh", "-c",
            "set -eu; test ! -e /root/.codex; test ! -e /home/c4gprobe/.codex; "
            "test ! -e /opt/c4g-tools/codex-runtime"])
        payload = root / "codex-runtime.tar"
        with tarfile.open(payload, "w") as archive:
            for name, data in sorted(files.items()):
                member = tarfile.TarInfo("codex-runtime/" + name)
                member.size = len(data)
                member.mode = 0o755 if data.startswith(b"\x7fELF") else 0o644
                member.uid = member.gid = 0
                archive.addfile(member, io.BytesIO(data))
        command = [native.executable, "-d", args.distro, "-u", "root", "--exec",
                   "/bin/tar", "--no-same-owner", "-xf", "-", "-C", "/opt/c4g-tools"]
        with payload.open("rb") as stream:
            subprocess.run(command, stdin=stream, check=True, timeout=180,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        executable = "/opt/c4g-tools/codex-runtime/bin/codex"
        version = native.exec(args.distro, [executable, "--version"]).stdout.decode().strip()
        if version != "codex-cli " + args.codex_version:
            raise RuntimeError("installed Codex version differs from the requested version")
        file_manifest = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                         for name, data in files.items()}
        verify = ("import hashlib,json,pathlib,sys; d=json.load(sys.stdin); "
                  "r=pathlib.Path('/opt/c4g-tools/codex-runtime'); "
                  "assert all((r/n).is_file() and not (r/n).is_symlink() and "
                  "(r/n).stat().st_size==v['bytes'] and "
                  "hashlib.sha256((r/n).read_bytes()).hexdigest()==v['sha256'] "
                  "for n,v in d.items()); print('verified')")
        native.exec(args.distro, [str(profile.python), "-I", "-B", "-c", verify],
                    input=json.dumps(file_manifest).encode())
        native.exec(args.distro, ["/bin/sh", "-c",
                    "test ! -e /root/.codex; test ! -e /home/c4gprobe/.codex"])
        native.terminate(args.distro)
        native._run(["--export", args.distro, str(baseline)], timeout=600)
        if sha256(profile.baseline) != profile.baseline_sha256:
            raise RuntimeError("parent baseline changed during Codex setup")
        agents = {name: str(path) for name, path in profile.agents.items()}
        agents["codex"] = executable
        profile_path = create_runtime_profile(args.profile_root, baseline, agents=agents)
        metadata = {"parent_baseline": str(profile.baseline),
                    "parent_baseline_sha256": profile.baseline_sha256,
                    "codex_version": args.codex_version, "codex_package_sha256": args.codex_package_sha256,
                    "codex_files": file_manifest, "agents": agents, "model_calls": 0}
        (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        record.update(status="exported", profile=str(profile_path), baseline_sha256=sha256(baseline))
        save()
        native.unregister(args.distro)
        install = (root / "distro").resolve()
        if install.parent != root:
            raise RuntimeError("unexpected Codex setup install path")
        if install.exists():
            install.rmdir()
        record["status"] = "complete"
        save()
        print(json.dumps(record, indent=2), flush=True)
        return 0
    except BaseException as exc:
        record.update(status="failed", error_type=type(exc).__name__)
        save()
        if imported:
            native.terminate(args.distro)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    runtime_root = REPO / "runtime" / "wsl2"
    parser.add_argument("--root", type=Path, default=runtime_root / "baseline")
    parser.add_argument("--profile-root", type=Path, default=runtime_root)
    parser.add_argument("--distro", default=DISTRO)
    parser.add_argument("--claude-source", type=Path,
                        help="Exact official Claude binary; no configuration directory is copied")
    parser.add_argument("--first-run", action="store_true",
                        help="Download the pinned Claude CLI and select the profile for a new checkout")
    parser.add_argument("--upgrade-run", type=Path,
                        help="Authorize a pinned Claude CLI update for a closed original development")
    parser.add_argument("--claude-version", help="Exact updated Claude CLI version")
    parser.add_argument("--claude-sha256", help="SHA-256 of the updated official Linux x64 executable")
    parser.add_argument("--baseline-claude-sha256", default=CLAUDE_SHA256,
                        help="Expected original baseline Claude executable SHA-256")
    parser.add_argument("--authorization-basis", help="Explicit user authorization for the CLI update")
    parser.add_argument("--base-profile", type=Path,
                        help="Clone an existing immutable baseline into a new Codex-capable baseline")
    parser.add_argument("--codex-package", type=Path, help="Official Linux x64 platform package TAR.GZ")
    parser.add_argument("--codex-package-sha256", help="Expected digest of the exact Codex platform package")
    parser.add_argument("--codex-version", help="Exact Codex CLI version to verify before export")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume-setup", action="store_true",
                        help="Continue only a marker-owned interrupted setup")
    args = parser.parse_args(argv)
    if args.first_run and (args.upgrade_run or args.base_profile):
        parser.error("--first-run is for the initial Claude Code baseline")
    if args.upgrade_run:
        if (args.base_profile or args.resume_setup or not args.claude_source or not args.claude_version
                or not args.claude_sha256 or not args.authorization_basis):
            parser.error("--upgrade-run requires a source, version, SHA-256 and authorization basis; other setup modes are forbidden")
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from coding4game_gui.wsl2.agent_upgrade import prepare_agent_upgrade
        result = prepare_agent_upgrade(args.upgrade_run, args.claude_source,
            version=args.claude_version, from_version=CLAUDE_VERSION, sha256=args.claude_sha256,
            baseline_sha256=args.baseline_claude_sha256, authorization_basis=args.authorization_basis,
            execute=args.execute)
        print(json.dumps(result, indent=2))
        return 0
    if args.base_profile:
        return extend_codex_baseline(args)
    plan = {"distro": args.distro, "location": str(args.root / "distro"),
            "baseline": str(args.root / "baseline.tar"), "packages": PACKAGES.split(),
            "claude_version": CLAUDE_VERSION, "claude_sha256": CLAUDE_SHA256,
            "credential_copy": False, "launch_game": False, "invoke_model": False}
    preset = None
    if args.first_run:
        import sys
        sys.path.insert(0, str(REPO))
        from coding4game_gui.operator_config import load_preset

        preset = REPO / "launcher.local.json"
        selected = configured_launcher_profile(REPO, load_preset(preset))
        if selected is not None:
            if not selected.is_file():
                parser.error(f"The launcher profile does not exist: {selected}. Check launcher.local.json.")
            print(f"Using the profile already set in launcher.local.json: {selected}")
            return 0
        plan.update(wsl_version=2, launcher_config=str(preset),
                    claude_download=CLAUDE_URL if args.claude_source is None else None,
                    claude_download_fallback=CLAUDE_NPM_URL if args.claude_source is None else None)
    print(json.dumps(plan, indent=2))
    if not args.execute: return 0

    baseline = args.root / "baseline.tar"
    prior = existing_profile(args.profile_root, baseline)
    if baseline.exists() or prior:
        if not baseline.is_file() or prior is None:
            raise RuntimeError("existing setup is incomplete; refusing to overwrite it")
        profile_path, profile = prior
        actual = sha256(baseline)
        if actual != profile.get("baseline_sha256"):
            raise RuntimeError("existing baseline/profile hash mismatch; refusing to overwrite")
        metadata_path = args.root / "metadata.json"
        if not metadata_path.is_file():
            raise RuntimeError("existing baseline metadata is missing")
        manifest = write_manifest(args.root, baseline, metadata_path)
        if preset is not None:
            select_first_run_profile(preset, profile_path)
        print(json.dumps({"reused": True, "baseline_sha256": actual,
                          "profile": str(profile_path), "manifest": str(manifest)}, indent=2))
        return 0

    claude_source = args.claude_source
    if claude_source is None and args.first_run:
        claude_source = download_claude(args.profile_root / "downloads")
    elif claude_source is None:
        raise RuntimeError("--claude-source is required for a new baseline")
    elif sha256(claude_source) != CLAUDE_SHA256:
        raise RuntimeError("Claude binary hash mismatch")
    args.root.mkdir(parents=True, exist_ok=True)

    existing = distros()
    marker_path = args.root / SETUP_MARKER
    if args.distro in existing:
        if not args.resume_setup:
            raise RuntimeError("distro already exists without explicit setup resume; refusing to modify it")
        read_resume_marker(marker_path, args.distro, args.root, baseline)
        if baseline.exists() or existing_profile(args.profile_root, baseline) is not None:
            raise RuntimeError("resume setup requires baseline and profile to be absent")
        registered = distro_base_path(args.distro)
        if canonical_windows_path(registered) != canonical_windows_path(args.root / "distro"):
            raise RuntimeError("registered distro BasePath does not match setup ownership marker")
        record_setup_stage(marker_path, "resume_started")
    elif args.resume_setup:
        raise RuntimeError("cannot resume setup because the owned distro is absent")
    else:
        marker_path = new_setup_marker(args.root, args.distro, baseline)
        record_setup_stage(marker_path, "install_started")
        try:
            version_args = ("--version", "2") if args.first_run else ()
            run("wsl.exe", "--install", "Debian", "--name", args.distro, "--location",
                str(args.root / "distro"), "--web-download", "--no-launch", *version_args)
        except Exception as exc:
            record_setup_stage(marker_path, "failed", setup_error_text(exc))
            raise
        record_setup_stage(marker_path, "install_completed")
    guest_script = f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y {PACKAGES}
if ! id c4gprobe >/dev/null 2>&1; then useradd -m -u 1000 -s /bin/bash c4gprobe; fi
actual_uid="$(id -u c4gprobe)" || {{ echo 'c4gprobe lookup failed after creation' >&2; exit 1; }}
test "$actual_uid" = 1000 || {{ echo "c4gprobe has unexpected uid: $actual_uid" >&2; exit 1; }}
install -d -o root -g root -m 0755 /opt/c4g-tools
test ! -e /root/.claude -a ! -e /root/.config/claude
test ! -e /home/c4gprobe/.claude -a ! -e /home/c4gprobe/.config/claude
cat > /etc/wsl.conf <<'EOF'
[automount]
enabled=false
[interop]
enabled=false
appendWindowsPath=false
[boot]
systemd=false
[user]
default=c4gprobe
[gpu]
enabled=false
EOF
printf '# disabled for immutable benchmark baseline\n' > /etc/fstab
"""
    try:
        run_guest_script(args.distro, guest_script)
        stream_claude(args.distro, claude_source)
        run("wsl.exe", "-d", args.distro, "-u", "root", "--", "bash", "-lc",
            f"printf '%s  %s\\n' '{CLAUDE_SHA256}' /opt/c4g-tools/claude | sha256sum -c -")
        client_dependencies = native_python_dependencies(args.distro)
        metadata_raw = subprocess.run(
        ["wsl.exe", "-d", args.distro, "-u", "root", "--", "bash", "-lc",
         "set -eu; printf '{'; printf '\"uid\":%s,' \"$(id -u c4gprobe)\"; "
         "printf '\"claude_sha256\":\"%s\",' \"$(sha256sum /opt/c4g-tools/claude|cut -d' ' -f1)\"; "
         "printf '\"claude_version\":\"%s\",' \"$(/opt/c4g-tools/claude --version | awk '{print $1}')\"; "
         "printf '\"python\":\"%s\"}' \"$(python3 --version 2>&1)\""],
            capture_output=True, text=True, check=True).stdout
        metadata = json.loads(metadata_raw)
        metadata["client_dependencies"] = client_dependencies
        (args.root / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        run("wsl.exe", "--terminate", args.distro)
        run("wsl.exe", "--export", args.distro, str(baseline))
    except Exception as exc:
        record_setup_stage(marker_path, "failed", setup_error_text(exc))
        raise
    try:
        baseline_hash = sha256(baseline)
        profile_path = create_runtime_profile(args.profile_root, baseline)
        manifest = write_manifest(args.root, baseline, args.root / "metadata.json")
        record_setup_stage(marker_path, "completed")
    except Exception as exc:
        record_setup_stage(marker_path, "failed", setup_error_text(exc))
        raise
    if preset is not None:
        select_first_run_profile(preset, profile_path)
    print(json.dumps({"baseline_sha256": baseline_hash, "profile": str(profile_path),
                      "manifest": str(manifest), "metadata": metadata}, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
