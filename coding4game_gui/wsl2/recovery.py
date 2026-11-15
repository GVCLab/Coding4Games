"""Verify and restore a development workspace without executing its contents."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import tarfile


class WorkspaceRecoveryError(ValueError):
    """An archive cannot safely restore the recorded development workspace."""


@dataclass(frozen=True)
class VerifiedWorkspace:
    workspace: PurePosixPath
    archive_sha256: str
    entries: dict
    payload: bytes = field(repr=False)

    def restore_input(self, uid):
        if type(uid) is not int or uid < 1000:
            raise WorkspaceRecoveryError("Invalid restored workspace owner")
        metadata = {"workspace": str(self.workspace), "uid": uid,
                    "entries": self.entries}
        return json.dumps(metadata, separators=(",", ":")).encode() + b"\n" + self.payload


def verify_workspace_archive(record, profile):
    """Bind immutable archive bytes to the original profile and workspace.

    The caller owns process/operation reconciliation. This function neither
    terminates a surviving run nor treats archive existence as a dead owner.
    """
    if not isinstance(record, dict):
        raise WorkspaceRecoveryError("Recovery archive descriptor is required")
    expected = f"C4G-WSL2-Run-{profile.id}-development-[0-9a-f]{{12}}"
    if (record.get("role") != "development"
            or record.get("profile_id") != profile.id
            or record.get("baseline_sha256") != profile.baseline_sha256
            or re.fullmatch(expected, str(record.get("distro", ""))) is None):
        raise WorkspaceRecoveryError("Recovery archive profile or development identity mismatch")
    workspace = str(record.get("workspace", ""))
    if re.fullmatch(f"/home/{profile.user}/run/[0-9a-f]{{24}}", workspace) is None:
        raise WorkspaceRecoveryError("Invalid original development workspace")
    path = Path(record["path"])
    if not path.is_file() or path.is_symlink():
        raise WorkspaceRecoveryError("Recovery archive must be a regular host file")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if (type(record.get("bytes")) is not int or len(payload) != record["bytes"]
            or digest != record.get("sha256")):
        raise WorkspaceRecoveryError("Recovery archive hash or byte count mismatch")
    prefix = PurePosixPath("run") / PurePosixPath(workspace).name
    entries = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                part = PurePosixPath(name)
                if (not name or "\\" in name or part.is_absolute()
                        or ".." in part.parts or part.as_posix() != name
                        or not (part == PurePosixPath("run") or part == prefix or prefix in part.parents)
                        or name in entries or member.mode & 0o6000
                        or ".c4g-dependencies" in part.parts):
                    raise WorkspaceRecoveryError("Unsafe or duplicate recovery archive member")
                value = {"mode": member.mode & 0o777}
                if member.isdir():
                    value["kind"] = "directory"
                elif member.isfile():
                    stream = archive.extractfile(member)
                    value.update(kind="file", bytes=member.size,
                                 sha256=hashlib.file_digest(stream, "sha256").hexdigest())
                elif member.issym() or member.islnk():
                    target = member.linkname
                    if not target or "\\" in target or "\x00" in target:
                        raise WorkspaceRecoveryError("Unsafe recovery archive link")
                    resolved = (posixpath.normpath(posixpath.join(str(part.parent), target))
                                if member.issym() else posixpath.normpath(target))
                    pinned_tools = {str(profile.python), *(str(path) for path in
                                    getattr(profile, "agents", {}).values())}
                    baseline_tool_link = member.issym() and target in pinned_tools
                    target_path = PurePosixPath(resolved)
                    if not baseline_tool_link and not (target_path == prefix or prefix in target_path.parents):
                        raise WorkspaceRecoveryError("Recovery archive link escapes workspace")
                    value.update(kind="symlink" if member.issym() else "hardlink", target=target)
                else:
                    raise WorkspaceRecoveryError("Special recovery archive member is forbidden")
                entries[name] = value
    except (tarfile.TarError, OSError) as error:
        raise WorkspaceRecoveryError("Unreadable recovery workspace archive") from error
    for root in ("run", prefix.as_posix()):
        if entries.get(root, {}).get("kind") != "directory":
            raise WorkspaceRecoveryError("Original workspace directory is absent")
    for name, value in entries.items():
        for parent in PurePosixPath(name).parents:
            if parent == PurePosixPath("."):
                break
            if entries.get(parent.as_posix(), {}).get("kind") != "directory":
                raise WorkspaceRecoveryError("Recovery member has a missing or linked parent")
        if value["kind"] == "hardlink":
            if entries.get(value["target"], {}).get("kind") != "file":
                raise WorkspaceRecoveryError("Recovery hardlink target is not a regular member")
    return VerifiedWorkspace(PurePosixPath(workspace), digest, entries, payload)


# Only this trusted installer executes before the private worker starts. Archive
# programmes and CLI history remain data until ordinary scoped launch preparation.
RESTORE_WORKSPACE = r'''
import hashlib, io, json, os, pathlib, stat, sys, tarfile
metadata=json.loads(sys.stdin.buffer.readline())
workspace=pathlib.Path(metadata['workspace']); base=pathlib.Path('/home/c4gprobe')
if workspace.exists() or workspace.is_symlink() or (base/'run').is_symlink():
    raise RuntimeError('Recovery target is not an empty baseline workspace')
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:') as archive:
    ordinary=[m for m in archive.getmembers() if m.isfile() or m.isdir()]
    archive.extractall(base,members=ordinary,filter='data')
entries=metadata['entries']; uid=metadata['uid']
for name, entry in entries.items():
    path=base/name
    if entry['kind']=='symlink':
        if pathlib.Path(entry['target']).is_absolute():
            target=pathlib.Path(entry['target']).resolve(strict=True); info=target.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
                raise RuntimeError('Pinned baseline tool link is not a read-only root-owned file')
        os.symlink(entry['target'],path)
    elif entry['kind']=='hardlink': os.link(base/entry['target'],path,follow_symlinks=False)
files=0
for name, entry in entries.items():
    path=base/name; mode=path.lstat().st_mode; kind=entry['kind']
    if kind in ('file','hardlink'):
        if not stat.S_ISREG(mode): raise RuntimeError('Restored file type mismatch')
        target=entries[entry['target']] if kind=='hardlink' else entry
        with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if path.stat().st_size!=target['bytes'] or digest!=target['sha256']:
            raise RuntimeError('Restored file bytes mismatch')
        files+=1
    elif kind=='directory':
        if not stat.S_ISDIR(mode): raise RuntimeError('Restored directory type mismatch')
    elif not stat.S_ISLNK(mode) or os.readlink(path)!=entry['target']:
        raise RuntimeError('Restored link mismatch')
    os.chown(path,uid,uid,follow_symlinks=False)
    if kind!='symlink': os.chmod(path,entry['mode'])
os.chmod(workspace,0o700)
print(json.dumps({'workspace':str(workspace),'verified_files':files,'verified_entries':len(entries)}))
'''
