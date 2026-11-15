import json

import pytest

from coding4game_gui.benchmark.code.submissions import ProgramStore


@pytest.mark.parametrize("size", [(768, 461), (1000, 1000)])
def test_viewport_is_part_of_program_identity_and_survives_copy(tmp_path, size):
    store = ProgramStore(tmp_path / "programs")
    files = {"main.py": b"pass"}
    entry = {"argv": ["main.py"], "mode": "stepped", "runtime": "python"}
    legacy = store.save(files, entry)
    small = store.save(files, entry, observation_size=size)
    assert small["id"] != legacy["id"]
    record, restored = store.get(small["id"])
    copied = ProgramStore(tmp_path / "final").save(
        restored, record["entrypoint"], observation_size=record["observation_size"])
    assert copied["id"] == small["id"]
    assert copied["observation_size"] == list(size)
    assert "observation_size" not in store.get(legacy["id"])[0]


def test_program_can_be_saved_reloaded_and_explicitly_finished(tmp_path):
    store = ProgramStore(tmp_path / "programs")
    record = store.save(
        {"main.py": b"print('ok')\n", "assets/data.bin": b"data"},
        {"argv": ["main.py", "--quiet"], "mode": "stepped", "runtime": "python"},
        description="first candidate",
        dependencies={"python": "3.12", "packages": {"numpy": "2.1.0"}},
    )

    loaded, files = store.get(record["id"])
    assert loaded == record
    assert files == {"main.py": b"print('ok')\n", "assets/data.bin": b"data"}
    assert store.final() is None
    assert store.finish(record["id"]) == record
    assert store.final() == record


def test_identity_excludes_timestamp_and_description_but_includes_program_contract(tmp_path):
    store = ProgramStore(tmp_path / "programs")
    files = {"run.py": b"print(1)"}
    entry = {"argv": ["run.py"], "mode": "stepped", "runtime": "python"}
    first = store.save(files, entry, description="candidate A", dependencies={"lock": "a"})
    second = store.save(files, entry, description="candidate B", dependencies={"lock": "a"})
    changed = store.save(files, entry, dependencies={"lock": "b"})

    assert first["id"] == second["id"]
    assert changed["id"] != first["id"]
    assert len(store.list()) == 2


def test_execution_platform_is_immutable_identity_and_legacy_remains_unchanged(tmp_path):
    store = ProgramStore(tmp_path / "programs")
    files = {"run.py": b"pass"}
    entry = {"argv": ["run.py"], "mode": "stepped", "runtime": "python"}
    legacy = store.save(files, entry)
    linux = store.save(files, entry, execution_platform="linux")
    windows = store.save(files, entry, execution_platform="windows")
    assert len({legacy["id"], linux["id"], windows["id"]}) == 3
    assert "execution_platform" not in store.get(legacy["id"])[0]
    assert store.get(linux["id"])[0]["execution_platform"] == "linux"
    with pytest.raises(ValueError, match="execution_platform"):
        store.save(files, entry, execution_platform="darwin")


@pytest.mark.parametrize("entry", [
    {"argv": [r"C:\\host\\run.py"], "mode": "stepped", "runtime": "python"},
    {"argv": ["../run.py"], "mode": "stepped", "runtime": "python"},
    {"argv": ["run.exe"], "mode": "stepped", "runtime": "python"},
    {"argv": ["run.py"], "mode": "stepped", "runtime": "native"},
    {"argv": ["run.py"], "mode": "live", "runtime": "python"},
])
def test_entrypoint_cannot_select_an_ambient_host_executable(tmp_path, entry):
    with pytest.raises(ValueError):
        ProgramStore(tmp_path / "programs").save({"run.py": b"pass"}, entry)


def test_program_rejects_windows_collisions_links_and_configured_size_limit(tmp_path):
    store = ProgramStore(tmp_path / "programs", max_bytes=4)
    entry = {"argv": ["run.py"], "mode": "stepped", "runtime": "python"}
    with pytest.raises(ValueError, match="duplicate Windows"):
        store.save({"RUN.py": b"a", "run.py": b"b"}, entry)
    with pytest.raises(ValueError, match="exceeds"):
        store.save({"run.py": b"12345"}, entry)
    with pytest.raises(ValueError, match="unsafe"):
        store.save({"run.py": b"x", "dir/../escape": b"x"}, entry)


def test_declared_dependencies_can_come_from_bundled_program_metadata(tmp_path):
    manifest = json.dumps({"dependencies": {"tools": [{"path": "tools/helper.exe", "sha256": "abc"}]}})
    files = {"run.py": b"pass", "tools/helper.exe": b"tool", "c4g-program.json": manifest.encode()}
    record = ProgramStore(tmp_path / "programs").save(
        files, {"argv": ["run.py"], "mode": "stepped", "runtime": "python"})

    assert record["dependencies"]["tools"][0]["path"] == "tools/helper.exe"
    assert record["reproducibility"]["dependencies_verified"] is False


def test_corruption_is_detected_and_finish_never_selects_best_implicitly(tmp_path):
    store = ProgramStore(tmp_path / "programs")
    entry = {"argv": ["run.py"], "mode": "stepped", "runtime": "python"}
    low = store.save({"run.py": b"print('low')"}, entry)
    high = store.save({"run.py": b"print('high')"}, entry)
    assert store.final() is None
    store.finish(low["id"])
    assert store.final()["id"] == low["id"]

    path = tmp_path / "programs" / low["id"] / "files" / "run.py"
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        store.get(low["id"])
    assert high["id"] != low["id"]
