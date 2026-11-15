import hashlib
import io
from pathlib import PurePosixPath
import tarfile
from types import SimpleNamespace

import pytest

from coding4game_gui.wsl2.recovery import (
    WorkspaceRecoveryError, verify_workspace_archive,
)


WORKSPACE = "0123456789abcdef01234567"
PREFIX = "run/" + WORKSPACE


def archive_descriptor(tmp_path, extra=None):
    path = tmp_path / "workspace.tar"
    with tarfile.open(path, "w") as archive:
        for name in ("run", PREFIX, PREFIX + "/.claude"):
            entry = tarfile.TarInfo(name)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o700
            archive.addfile(entry)
        entry = tarfile.TarInfo(PREFIX + "/.claude/session.jsonl")
        payload = b'{"session":"preserved"}\n'
        entry.size, entry.mode = len(payload), 0o600
        archive.addfile(entry, io.BytesIO(payload))
        if extra:
            archive.addfile(extra, io.BytesIO(b"x" * extra.size))
    profile = SimpleNamespace(id="0123456789ab", user="c4gprobe",
                              baseline_sha256="b" * 64,
                              agents={"codex": PurePosixPath("/opt/c4g-tools/codex")},
                              python=PurePosixPath("/usr/bin/python3"))
    descriptor = {"path": str(path), "bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "distro": "C4G-WSL2-Run-0123456789ab-development-abcdef123456",
                  "role": "development", "profile_id": profile.id,
                  "baseline_sha256": profile.baseline_sha256,
                  "workspace": "/home/c4gprobe/" + PREFIX}
    return descriptor, profile


def test_archive_keeps_original_workspace_and_official_session_bytes(tmp_path):
    descriptor, profile = archive_descriptor(tmp_path)
    restored = verify_workspace_archive(descriptor, profile)
    assert str(restored.workspace) == descriptor["workspace"]
    entry = restored.entries[PREFIX + "/.claude/session.jsonl"]
    assert entry["sha256"] == hashlib.sha256(b'{"session":"preserved"}\n').hexdigest()
    assert restored.archive_sha256 == descriptor["sha256"]
    assert restored.restore_input(20001).endswith(restored.payload)
    assert "preserved" not in repr(restored)


@pytest.mark.parametrize("change", [
    {"baseline_sha256": "a" * 64}, {"profile_id": "f" * 12},
    {"role": "final_test"}, {"workspace": "/home/c4gprobe/run/../elsewhere"},
    {"sha256": "a" * 64}, {"bytes": 1},
    {"distro": "C4G-WSL2-Run-0123456789ab-final_test-abcdef123456"},
])
def test_changed_identity_or_archive_is_rejected(tmp_path, change):
    descriptor, profile = archive_descriptor(tmp_path)
    descriptor.update(change)
    with pytest.raises(WorkspaceRecoveryError):
        verify_workspace_archive(descriptor, profile)


@pytest.mark.parametrize("name", [
    "/etc/passwd", PREFIX + "/../../escape", "run/another-workspace/file",
    PREFIX + "/.c4g-dependencies/wheel", PREFIX + "/.claude/session.jsonl",
    PREFIX + "/missing-parent/file", PREFIX + "/file\\escape",
])
def test_unsafe_duplicate_or_unscoped_members_are_rejected(tmp_path, name):
    entry = tarfile.TarInfo(name)
    entry.size = 1
    descriptor, profile = archive_descriptor(tmp_path, entry)
    with pytest.raises(WorkspaceRecoveryError):
        verify_workspace_archive(descriptor, profile)


@pytest.mark.parametrize("kind,target", [
    (tarfile.SYMTYPE, "/etc/shadow"), (tarfile.SYMTYPE, "../../../outside"),
    (tarfile.LNKTYPE, "/etc/shadow"), (tarfile.LNKTYPE, PREFIX + "/absent"),
    (tarfile.CHRTYPE, ""), (tarfile.FIFOTYPE, ""),
])
def test_escaping_links_and_special_files_are_rejected(tmp_path, kind, target):
    entry = tarfile.TarInfo(PREFIX + "/link")
    entry.type, entry.linkname = kind, target
    descriptor, profile = archive_descriptor(tmp_path, entry)
    with pytest.raises(WorkspaceRecoveryError):
        verify_workspace_archive(descriptor, profile)


@pytest.mark.parametrize("target", [".claude/session.jsonl", "/usr/bin/python3", "/opt/c4g-tools/codex"])
def test_safe_internal_and_pinned_interpreter_symlinks_are_preserved(tmp_path, target):
    entry = tarfile.TarInfo(PREFIX + "/link")
    entry.type, entry.linkname = tarfile.SYMTYPE, target
    descriptor, profile = archive_descriptor(tmp_path, entry)
    value = verify_workspace_archive(descriptor, profile).entries[PREFIX + "/link"]
    assert value["kind"] == "symlink" and value["target"] == target
