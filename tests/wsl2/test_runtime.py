import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest
import subprocess
import sys

from coding4game_gui.wsl2 import WslProfile, WslRuntime
from coding4game_gui.wsl2.runtime import WslIsolationError


def profile_data(tmp_path):
    baseline = tmp_path / "baseline.tar"
    baseline.write_bytes(b"baseline")
    return {
        "format": "c4g-wsl2-v1", "id": "0123456789ab",
        "root": str(tmp_path / "runtime-0123456789ab"),
        "baseline": str(baseline),
        "baseline_sha256": hashlib.sha256(b"baseline").hexdigest(),
        "user":"c4gprobe", "uid":1000, "python": "/usr/bin/python3", "agents": {"claude": "/opt/c4g-tools/claude"},
        "ports": {"development_control": 51001, "development_game": 51002,
                  "final_test_control": 51003, "final_test_game": 51004, "gateway": 51005},
    }


def test_profile_loads_strict_linux_paths_and_identity(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile_data(tmp_path)), encoding="utf-8")
    p = WslProfile.load(path)
    assert p.backend == "wsl2" and p.execution_platform == "linux"
    assert p.python == PurePosixPath("/usr/bin/python3")
    assert p.agent("claude") == PurePosixPath("/opt/c4g-tools/claude")
    assert p.development_tools is None
    assert p.control_port("final_test") == 51003
    assert p.box("development").startswith("C4G-WSL2-Run-")


@pytest.mark.parametrize("change", [
    {"id": "BAD"}, {"python": "python3"}, {"agents": {"gemini": "/bin/gemini"}},
    {"ports": {"development_control": 1}}, {"baseline_sha256": "0" * 63},
])
def test_profile_rejects_unsafe_or_incomplete_values(tmp_path, change):
    data = profile_data(tmp_path); data.update(change)
    with pytest.raises(ValueError): WslProfile(data)


class FakeNative:
    def __init__(self, profile): self.profile = profile; self.calls = []; self.distros = set()
    def verify_host(self): return {"wsl_version": 2}
    def list_distros(self): return set(self.distros)
    def network_namespace(self,name): return "net:[1]"
    def candidate_uid(self,name): return 25000
    def uid_available(self,name,uid): return True
    def import_distro(self, name, install, baseline): self.calls.append(("import", name)); self.distros.add(name)
    def configure(self, name, **kwargs):
        self.calls.append(("configure", name, kwargs)); return {"cgroup":"/sys/fs/cgroup/fake","table":"fake"}
    def verify(self, name, **kwargs): self.calls.append(("verify", name, kwargs)); return {"uid": 1000, "network_canary": "blocked"}
    def terminate(self, name): self.calls.append(("terminate", name))
    def teardown(self,name,isolation): self.calls.append(("teardown",name,isolation))
    def unregister(self, name): self.calls.append(("unregister", name)); self.distros.discard(name)
    def exec(self, name, argv, **kwargs):
        self.calls.append(("exec", name, argv))
        if kwargs.get("stdout") is not None: kwargs["stdout"].write(b"export")
        return subprocess.CompletedProcess(argv, 0, stdout=b"directory\n")


class FakeSupervisor:
    def __init__(self, runtime, role, distro): self.runtime=runtime; self.role=role; self.distro=distro; self.closed=False
    def close(self): self.closed=True


class ReadyFakeSupervisor(FakeSupervisor):
    def __init__(self,*args):
        super().__init__(*args)
        self.control_listener_readiness={"host":"127.0.0.1","port":51001,
                                         "attempts":2,"bind_wait_seconds":0.1}


@pytest.mark.parametrize("role,recovery", [("final_test", {}), ("development", None), ("development", {})])
def test_agent_upgrade_never_starts_an_unverified_or_final_worker(tmp_path, role, recovery):
    profile = WslProfile(profile_data(tmp_path))
    native = FakeNative(profile)
    with WslRuntime(profile, native=native, supervisor_factory=FakeSupervisor) as runtime:
        with pytest.raises(WslIsolationError, match="verified development recovery authorization"):
            runtime.start(role, recovery=recovery, agent_upgrade={})
    assert native.calls == []


def test_runtime_hash_checks_and_imports_unique_clone_per_role(tmp_path):
    p = WslProfile(profile_data(tmp_path)); n = FakeNative(p)
    r = WslRuntime(p, native=n, supervisor_factory=FakeSupervisor, evidence_dir=tmp_path / "evidence")
    assert r.preflight()["security_claim"].startswith("host and guest checks")
    with r:
        dev = r.start("development"); assert r.start("development") is dev
        final = r.start("final_test")
        assert dev.distro != final.distro and dev.distro.startswith("C4G-WSL2-Run-")
    assert not n.distros
    assert [c[0] for c in n.calls].count("import") == 2
    verify_calls=[call for call in n.calls if call[0]=="verify"]
    assert all(call[2]["python"]==PurePosixPath("/usr/bin/python3") for call in verify_calls)


def test_runtime_refuses_changed_baseline_before_native_mutation(tmp_path):
    p = WslProfile(profile_data(tmp_path)); p.baseline.write_bytes(b"changed")
    n = FakeNative(p); r = WslRuntime(p, native=n, supervisor_factory=FakeSupervisor)
    with pytest.raises(WslIsolationError, match="hash"):
        r.preflight()
    assert n.calls == []


def test_runtime_preserves_clone_when_export_fails(tmp_path, monkeypatch):
    p = WslProfile(profile_data(tmp_path)); n = FakeNative(p)
    r = WslRuntime(p, native=n, supervisor_factory=FakeSupervisor, evidence_dir=tmp_path / "evidence")
    monkeypatch.setattr(r, "_export", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(WslIsolationError, match="cleanup"):
        with r: r.start("development")
    assert any(name.startswith("C4G-WSL2-Run-") for name in n.distros)


def test_failed_initialization_without_workspace_exports_empty_archive_and_cleans(tmp_path):
    p = WslProfile(profile_data(tmp_path)); n = FakeNative(p)
    n.verify = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verification failed"))
    original_exec = n.exec
    def absent_workspace(name, argv, **kwargs):
        completed = original_exec(name, argv, **kwargs)
        if argv[:2] == ["/bin/sh", "-c"]:
            completed.stdout = b"absent\n"
        return completed
    n.exec = absent_workspace
    r = WslRuntime(p, native=n, evidence_dir=tmp_path / "evidence")
    with pytest.raises(RuntimeError, match="verification failed"):
        with r: r.start("development")
    assert not n.distros
    assert r.archives[0]["workspace_state"] == "absent"
    assert any(call[0] == "exec" and "--files-from" in call[2] for call in n.calls)


def test_started_workspace_disappearance_preserves_clone(tmp_path):
    p = WslProfile(profile_data(tmp_path)); n = FakeNative(p)
    original_exec = n.exec
    def absent_workspace(name, argv, **kwargs):
        completed = original_exec(name, argv, **kwargs)
        if argv[:2] == ["/bin/sh", "-c"]:
            completed.stdout = b"absent\n"
        return completed
    n.exec = absent_workspace
    r = WslRuntime(p, native=n, supervisor_factory=FakeSupervisor)
    with pytest.raises(WslIsolationError, match="cleanup failed"):
        with r: r.start("development")
    assert n.distros
    assert not r.archives


def test_export_excludes_only_reconstructable_dependency_roots_and_records_sidecar(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p)
    r=WslRuntime(p,native=n,evidence_dir=tmp_path/"evidence")
    record=r._export("final_test","C4G-WSL2-Run-test-final_test-abcdefabcdef")
    tar_call=next(call for call in n.calls if call[0]=="exec" and call[2][0]=="/bin/tar")
    argv=tar_call[2]
    assert "--exclude=run/*/.c4g-dependencies" in argv
    assert [value for value in argv if value.startswith("--exclude=")]==[
        "--exclude=run/*/.c4g-dependencies"]
    assert record["excluded_reconstructable_paths"]==["run/*/.c4g-dependencies"]
    sidecar=json.loads(Path(record["path"]).with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["excluded_reconstructable_paths"]==["run/*/.c4g-dependencies"]


def test_export_rejects_symlinked_dependency_root_before_tar(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p)
    original=n.exec
    def unsafe_probe(name,argv,**kwargs):
        if argv[:2]==["/bin/sh","-c"]:
            n.calls.append(("exec",name,argv)); return subprocess.CompletedProcess(argv,0,stdout=b"unsafe\n")
        return original(name,argv,**kwargs)
    n.exec=unsafe_probe
    r=WslRuntime(p,native=n,evidence_dir=tmp_path/"evidence")
    with pytest.raises(WslIsolationError,match="safe directory"):
        r._export("final_test","C4G-WSL2-Run-test-final_test-abcdefabcdef")
    assert not any(call[0]=="exec" and call[2][0]=="/bin/tar" for call in n.calls)
    probe=next(call[2][2] for call in n.calls if call[0]=="exec" and call[2][:2]==["/bin/sh","-c"])
    assert "-mindepth 2 -maxdepth 2 -name .c4g-dependencies -type l" in probe


def test_os_held_lock_survives_stale_file_and_excludes_live_owner(tmp_path):
    p=WslProfile(profile_data(tmp_path)); p.root.mkdir(parents=True)
    lock=p.root/"run.lock"; lock.write_text("stale")
    first=WslRuntime(p,native=FakeNative(p),supervisor_factory=FakeSupervisor)
    second=WslRuntime(p,native=FakeNative(p),supervisor_factory=FakeSupervisor)
    with first:
        with pytest.raises(WslIsolationError,match="already in use"): second.__enter__()
    assert lock.is_file()
    with second: pass


def test_host_crash_releases_byte_lock_without_deleting_lock_file(tmp_path):
    p=WslProfile(profile_data(tmp_path)); p.root.mkdir(parents=True); lock=p.root/"run.lock"
    script=("import msvcrt,os,sys; f=open(sys.argv[1],'a+b'); f.seek(0); "
            "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); f.write(b'crashed'); f.flush(); os._exit(7)")
    assert subprocess.run([sys.executable,"-c",script,str(lock)]).returncode==7
    with WslRuntime(p,native=FakeNative(p),supervisor_factory=FakeSupervisor): pass
    assert lock.is_file()


def test_topology_change_fails_before_configuration_and_records_clone(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p); n.distros.add("C4G-WSL2-Probe-20261013")
    n.network_namespace=lambda name:"net:[2]" if "Run-" in name else "net:[1]"
    r=WslRuntime(p,native=n,supervisor_factory=FakeSupervisor); r.preflight()
    r.__enter__()
    with pytest.raises(WslIsolationError,match="topology"): r.start("development")
    r._lock.close(); r._lock=None
    assert not any(call[0]=="configure" for call in n.calls)
    records=list((p.root/"clone_records").glob("*.json"))
    assert json.loads(records[0].read_text())["status"]=="topology_mismatch"


def test_dependency_failure_prevents_supervisor_start(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p)
    n.verify=lambda name,**kwargs: (_ for _ in ()).throw(RuntimeError("No module named cv2"))
    r=WslRuntime(p,native=n,supervisor_factory=lambda *a:pytest.fail("supervisor must not start"))
    r.__enter__()
    with pytest.raises(RuntimeError,match="cv2"): r.start("development")
    r._lock.close(); r._lock=None


def test_role_evidence_contains_only_verified_nonsecret_runtime_identity(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p)
    r=WslRuntime(p,native=n,supervisor_factory=FakeSupervisor); r.preflight()
    with r:
        r.start("development")
        evidence=r.role_evidence("development")
        assert evidence["role"]=="development"
        assert evidence["profile_id"]==p.id
        assert evidence["baseline_sha256"]==p.baseline_sha256
        assert evidence["network_namespace"]=="net:[1]"
        assert evidence["network_topology"]=="reference_unavailable"
        assert evidence["isolation"]["network_canary"]=="blocked"
        serialized=json.dumps(evidence).lower()
        assert "token" not in serialized and "credential" not in serialized and "secret" not in serialized


def test_role_evidence_includes_safe_control_listener_readiness(tmp_path):
    p=WslProfile(profile_data(tmp_path)); n=FakeNative(p)
    r=WslRuntime(p,native=n,supervisor_factory=ReadyFakeSupervisor); r.preflight()
    with r:
        r.start("development")
        assert r.role_evidence("development")["control_listener"]=={
            "host":"127.0.0.1","port":51001,"attempts":2,"bind_wait_seconds":0.1}


def test_development_restore_verifies_before_import_and_preserves_workspace(tmp_path):
    import io
    import tarfile
    from coding4game_gui.wsl2.recovery import WorkspaceRecoveryError
    p = WslProfile(profile_data(tmp_path))
    n = FakeNative(p)
    original_configure, original_exec = n.configure, n.exec
    n.configure = lambda *a, **k: original_configure(*a, **k) | {"uid": 25000}
    def execute(name, argv, **kwargs):
        if argv[0] == str(p.python):
            meta = json.loads(kwargs["input"].split(b"\n", 1)[0])
            n.calls.append(("restore", name, meta))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
                "workspace": meta["workspace"], "verified_files": 1,
                "verified_entries": len(meta["entries"])}).encode())
        return original_exec(name, argv, **kwargs)
    n.exec = execute
    root = "run/" + "a" * 24
    archive = tmp_path / "original.tar"
    with tarfile.open(archive, "w") as stream:
        for value in ("run", root):
            member = tarfile.TarInfo(value); member.type = tarfile.DIRTYPE
            stream.addfile(member)
        member = tarfile.TarInfo(root + "/candidate.py"); member.size = 4
        stream.addfile(member, io.BytesIO(b"pass"))
    descriptor = {"role": "development", "profile_id": p.id,
                  "baseline_sha256": p.baseline_sha256,
                  "distro": f"C4G-WSL2-Run-{p.id}-development-abcdef123456",
                  "path": str(archive), "bytes": archive.stat().st_size,
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "workspace": "/home/c4gprobe/" + root}
    runtime = WslRuntime(p, native=n, supervisor_factory=FakeSupervisor,
                         evidence_dir=tmp_path / "evidence")
    with runtime:
        with pytest.raises(WorkspaceRecoveryError, match="hash"):
            runtime.start("development", recovery=descriptor | {"sha256": "0" * 64})
        assert not n.calls
        with pytest.raises(WslIsolationError, match="clean workspace"):
            runtime.start("final_test", recovery=descriptor)
        assert not n.calls
        runtime.start("development", recovery=descriptor)
        assert str(runtime._restored_workspaces["development"]) == descriptor["workspace"]
        assert runtime.role_evidence("development")["recovery"]["archive_sha256"] == descriptor["sha256"]
        with pytest.raises(WslIsolationError, match="live supervisor"):
            runtime.start("development", recovery=descriptor)
    assert not n.distros
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == descriptor["sha256"]
