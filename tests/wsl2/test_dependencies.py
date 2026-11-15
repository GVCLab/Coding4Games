import hashlib
import json
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from coding4game_gui.wsl2.dependencies import DependencyManager
from coding4game_gui.wsl2.dependency_manifest import lock_id


BASELINE = "a" * 64
WHEEL_BYTES = b"locked-wheel"
WHEEL_HASH = hashlib.sha256(WHEEL_BYTES).hexdigest()


def locked_environment(*, baseline=BASELINE):
    return {
        "format": "c4g-wsl-env-lock-v1",
        "baseline_sha256": baseline,
        "python": "3.13.5",
        "platform": "linux_x86_64",
        "requirements": ["demo==1.0"],
        "indexes": ["https://pypi.org/simple"],
        "wheels": [{"filename": "demo-1.0-py3-none-any.whl", "name": "demo",
                    "version": "1.0", "sha256": WHEEL_HASH,
                    "bytes": len(WHEEL_BYTES)}],
        "assets": [],
    }


class Native:
    def __init__(self, transitions, fail_seal=False):
        self.transitions = transitions
        self.fail_seal = fail_seal

    def set_dependency_network(self, distro, isolation, *, public):
        assert self.owner._dependency_preparing is True
        assert self.owner._final_execution_started is False
        self.transitions.append(public)
        if not public and self.fail_seal:
            raise RuntimeError("network seal failed")
        return {"public": public}


class Supervisor:
    def __init__(self, tmp_path, *, role="final_test", fail_seal=False):
        self.role = role
        self.distro = "clean-final"
        self.workspace = PurePosixPath("/home/c4gprobe/run")
        self._final_execution_started = False
        self._dependency_preparing = False
        self._dependency_failed = False
        self.transitions = []
        profile = SimpleNamespace(
            root=tmp_path / "runtime-0123456789ab",
            baseline_sha256=BASELINE,
            python=PurePosixPath("/usr/bin/python3"),
        )
        native = Native(self.transitions, fail_seal=fail_seal)
        native.owner = self
        self.runtime = SimpleNamespace(
            profile=profile,
            evidence_dir=tmp_path / "evidence",
            native=native,
            _cgroups={"final_test": {"uid": 21000, "cgroup": "/sys/fs/cgroup/c4g"}},
        )


def manager_with_installer(tmp_path, *, fail_install=False, fail_verify=False,
                           fail_seal=False):
    supervisor = Supervisor(tmp_path, fail_seal=fail_seal)
    manager = DependencyManager(supervisor)
    manager.cache.put(WHEEL_HASH, len(WHEEL_BYTES), [WHEEL_BYTES])
    install_root = PurePosixPath("/home/c4gprobe/run/.c4g-dependencies/env")
    manager._stage_request = lambda request, name: install_root
    materialized = []
    manager._materialize = lambda item, path, deadline=float("inf"): (
        manager.cache.contains(item["sha256"], item["bytes"]),
        materialized.append((item["sha256"], str(path))),
    )
    actions = []

    def run(action, directory, deadline):
        assert supervisor._dependency_preparing is True
        assert supervisor._final_execution_started is False
        actions.append(action)
        if action == "install":
            assert supervisor.transitions == [True]
        elif action == "verify":
            assert supervisor.transitions == [True, False]
        else:
            raise AssertionError(action)
        if action == "install" and fail_install:
            raise RuntimeError("installer failed")
        if action == "verify" and fail_verify:
            raise RuntimeError("verification failed")

    manager._run = run

    def read(path):
        name = PurePosixPath(path).name
        if name == "installed.json":
            return json.dumps({"verified": True,
                               "python": "/home/c4gprobe/run/.venv/bin/python"}).encode()
        return b"verified evidence\n"

    manager._read = read
    return manager, supervisor, materialized, actions


def test_final_prepare_opens_network_before_installer_then_always_seals_and_maps_runtime(tmp_path):
    manager, supervisor, materialized, actions = manager_with_installer(tmp_path)
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    result = manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == [True, False]
    assert actions == ["install", "verify"]
    assert supervisor._dependency_preparing is False
    assert supervisor._dependency_failed is False
    assert result["python"] == "/home/c4gprobe/run/.venv/bin/python"
    assert result["env"] == {
        "C4G_ASSET_DIR": "/home/c4gprobe/run/.c4g-dependencies/env/assets"}
    assert result["verified"] is True
    assert result["lock_id"] == lock_id(locked_environment())
    assert materialized == [(WHEEL_HASH,
        "/home/c4gprobe/run/.c4g-dependencies/env/wheels/demo-1.0-py3-none-any.whl")]


def test_final_prepare_seals_network_after_installer_failure(tmp_path):
    manager, supervisor, _, actions = manager_with_installer(tmp_path, fail_install=True)
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    with pytest.raises(RuntimeError, match="installer failed"):
        manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == [True, False]
    assert actions == ["install"]
    assert supervisor._dependency_preparing is False
    assert supervisor._dependency_failed is True


def test_final_prepare_refuses_to_start_after_final_execution(tmp_path):
    manager, supervisor, _, actions = manager_with_installer(tmp_path)
    supervisor._final_execution_started = True
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    with pytest.raises(RuntimeError, match="precede final execution"):
        manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == []
    assert actions == []


def test_final_network_seal_failure_marks_dependency_state_failed(tmp_path):
    manager, supervisor, _, actions = manager_with_installer(tmp_path, fail_seal=True)
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    with pytest.raises(RuntimeError, match="network seal failed"):
        manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == [True, False]
    assert actions == ["install"]
    assert supervisor._dependency_preparing is False
    assert supervisor._dependency_failed is True


def test_prepare_rejects_wrong_baseline_before_network_or_install(tmp_path):
    manager, supervisor, _, actions = manager_with_installer(tmp_path)
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment(baseline="b" * 64)}}

    with pytest.raises(ValueError, match="baseline does not match"):
        manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == []
    assert actions == []


def test_prepare_detects_corrupt_shared_artifact_before_installer(tmp_path):
    manager, supervisor, _, _ = manager_with_installer(tmp_path)
    manager.cache.path(WHEEL_HASH).write_bytes(b"corrupt-data")
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    # Exercise the real materializer so ArtifactCache performs both size and hash checks.
    with pytest.raises(ValueError, match="cached artifact (size|hash) mismatch"):
        DependencyManager._materialize(
            manager, locked_environment()["wheels"][0], PurePosixPath("/guest/wheel"))

    assert supervisor.transitions == []


def test_final_verification_failure_after_network_seal_blocks_execution(tmp_path):
    manager, supervisor, _, actions = manager_with_installer(
        tmp_path, fail_verify=True)
    record = {"id": "program-1", "dependencies": {
        "environment_lock": locked_environment()}}

    with pytest.raises(RuntimeError, match="verification failed"):
        manager.prepare(record, PurePosixPath("/program"), float("inf"))

    assert supervisor.transitions == [True, False]
    assert actions == ["install", "verify"]
    assert supervisor._dependency_preparing is False
    assert supervisor._dependency_failed is True


def test_old_metadata_without_environment_uses_baseline_without_network(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)

    result = manager.prepare(
        {"id": "legacy", "dependencies": {"note": "descriptive"}},
        PurePosixPath("/program"), float("inf"))

    assert result == {"python": "/usr/bin/python3", "env": {}, "verified": False}
    assert supervisor.transitions == []
    assert supervisor._dependency_preparing is False


def test_freeze_keeps_local_asset_source_only_in_request_evidence(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    source_path = "/home/c4gprobe/run/random-workspace/models/model.bin"
    asset_bytes = b"model"
    asset_hash = hashlib.sha256(asset_bytes).hexdigest()
    staged_requests = []
    records = []
    manager._stage_request = lambda request, name: (
        staged_requests.append(request) or PurePosixPath("/guest/resolve"))
    manager._run = lambda action, directory, deadline: None
    resolved = {
        "python": "3.13.5",
        "platform": "linux_x86_64",
        "wheels": locked_environment()["wheels"],
        "assets": [{"path": "models/model.bin", "url": None,
                    "source_path": source_path, "sha256": asset_hash,
                    "bytes": len(asset_bytes)}],
    }
    manager._read = lambda path: json.dumps(resolved).encode()
    cached = []
    manager._cache_file = lambda item, source, deadline=float("inf"): cached.append(
        (item, str(source)))
    manager._record = lambda name, value: records.append((name, value))

    frozen = manager.freeze({}, {"environment": {
        "requirements": ["demo==1.0"],
        "assets": [{"path": "models/model.bin", "source_path": source_path,
                    "sha256": asset_hash}],
    }}, float("inf"))

    assert frozen["environment"] == {
        "requirements": ["demo==1.0"], "indexes": ["https://pypi.org/simple"]}
    assert frozen["environment_lock"]["assets"] == [{
        "path": "models/model.bin", "url": None, "sha256": asset_hash,
        "bytes": len(asset_bytes)}]
    assert source_path not in json.dumps(frozen, sort_keys=True)
    assert staged_requests[0]["environment"]["assets"][0]["source_path"] == source_path
    assert any(name.startswith("request-") and
               value["assets"][0]["source_path"] == source_path
               for name, value in records)
    assert cached[-1][1] == source_path


def remote_lock():
    asset_bytes = b"remote-model"
    asset_hash = hashlib.sha256(asset_bytes).hexdigest()
    lock = locked_environment()
    lock["assets"] = [{"path": "models/model.bin",
                       "url": "https://example.org/model.bin",
                       "sha256": asset_hash, "bytes": len(asset_bytes)}]
    return lock, asset_bytes


def test_hydrate_downloads_only_missing_exact_lock_artifacts_and_reuses_cache(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    lock, asset_bytes = remote_lock()
    requests = []
    runs = []
    manager._stage_request = lambda request, name: (
        requests.append(request) or PurePosixPath("/guest/restore"))
    manager._run = lambda action, directory, deadline: runs.append(action)
    resolved = {
        "wheels": lock["wheels"],
        "assets": [{**lock["assets"][0],
                    "source_path": "/guest/download/model.bin"}],
    }
    manager._read = lambda path: json.dumps(resolved).encode()

    def chunks(path, size, deadline=float("inf")):
        data = (WHEEL_BYTES if PurePosixPath(path).suffix == ".whl"
                else asset_bytes)
        assert len(data) == size
        yield data

    manager._chunks = chunks

    manager._hydrate(lock, float("inf"))
    manager._hydrate(lock, float("inf"))

    assert runs == ["resolve"]
    assert len(requests) == 1
    assert requests[0]["frozen_download"] is True
    assert requests[0]["environment"] == {
        "requirements": ["demo==1.0"],
        "indexes": ["https://pypi.org/simple"],
        "assets": [{"path": "models/model.bin",
                    "url": "https://example.org/model.bin",
                    "sha256": lock["assets"][0]["sha256"]}],
    }
    assert manager.cache.contains(WHEEL_HASH, len(WHEEL_BYTES))
    assert manager.cache.contains(lock["assets"][0]["sha256"], len(asset_bytes))


@pytest.mark.parametrize(("field", "replacement", "message"), [
    ("name", "other", "frozen hash"),
    ("version", "2.0", "frozen hash"),
    ("sha256", "f" * 64, "frozen hash"),
    ("bytes", len(WHEEL_BYTES) + 1, "frozen hash"),
])
def test_hydrate_rejects_downloaded_wheel_metadata_different_from_lock(
        tmp_path, field, replacement, message):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    lock = locked_environment()
    manager._stage_request = lambda request, name: PurePosixPath("/guest/restore")
    manager._run = lambda action, directory, deadline: None
    changed = dict(lock["wheels"][0], **{field: replacement})
    manager._read = lambda path: json.dumps(
        {"wheels": [changed], "assets": []}).encode()

    with pytest.raises(ValueError, match=message):
        manager._hydrate(lock, float("inf"))

    assert not manager.cache.path(WHEEL_HASH).exists()


def test_hydrate_rejects_different_wheel_filename_set(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    lock = locked_environment()
    manager._stage_request = lambda request, name: PurePosixPath("/guest/restore")
    manager._run = lambda action, directory, deadline: None
    manager._read = lambda path: json.dumps({
        "wheels": [{**lock["wheels"][0], "filename": "other.whl"}],
        "assets": [],
    }).encode()

    with pytest.raises(ValueError, match="downloaded wheels differ"):
        manager._hydrate(lock, float("inf"))


def test_hydrate_missing_local_asset_fails_without_download(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    lock = locked_environment()
    lock["assets"] = [{"path": "models/local.bin", "url": None,
                       "sha256": "e" * 64, "bytes": 10}]
    manager._stage_request = lambda *args: pytest.fail("must not download")
    manager._run = lambda *args: pytest.fail("must not resolve")

    with pytest.raises(ValueError, match="captured local asset is missing"):
        manager._hydrate(lock, float("inf"))


def test_hydrate_transfer_deadline_prevents_cache_write(tmp_path):
    supervisor = Supervisor(tmp_path, role="development")
    manager = DependencyManager(supervisor)
    lock = locked_environment()
    manager._stage_request = lambda request, name: PurePosixPath("/guest/restore")
    manager._run = lambda action, directory, deadline: None
    manager._read = lambda path: json.dumps(
        {"wheels": lock["wheels"], "assets": []}).encode()
    supervisor.call = lambda *args, **kwargs: pytest.fail("deadline must precede transfer RPC")

    with pytest.raises(TimeoutError, match="transfer deadline"):
        manager._hydrate(lock, 0)

    assert not manager.cache.path(WHEEL_HASH).exists()
