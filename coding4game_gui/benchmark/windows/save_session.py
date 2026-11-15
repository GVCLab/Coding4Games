"""Recoverable protection for host player saves during benchmark runs.

Golden checkpoints deliberately overwrite the registered save directory.  A
scenario must opt in with ``startup.preserve_player_save: true`` before that is
allowed in a formal runner.  The original tree is copied into run evidence and
verified before play; the generated tree is retained as evidence and the
original is restored byte-for-byte when the outer run exits.
"""
from __future__ import annotations

from contextlib import contextmanager
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from coding4game_gui.benchmark.windows import save_state
from coding4game_gui.paths import no_reparse

def _extended(path: Path) -> Path:
    return Path("\\\\?\\" + str(path)) if os.name == "nt" and not str(path).startswith("\\\\?\\") else path


def _canonical(path: Path) -> Path:
    """Compare ordinary and extended local Windows spellings identically."""
    value = str(path)
    if os.name == 'nt' and value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(value).resolve()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_manifest(path: Path) -> dict:
    """Return a stable per-file manifest and aggregate digest for one path."""
    path = Path(path)
    if not path.exists():
        return {"kind": "absent", "files": [], "sha256": None}
    no_reparse(path)
    if path.is_symlink():
        raise RuntimeError(f"save preservation refuses a symlink: {path}")
    if path.is_file():
        files = [{"path": ".", "size": path.stat().st_size,
                  "sha256": _file_sha256(path)}]
        directories = []
        kind = "file"
    elif path.is_dir():
        files = []
        directories = []
        for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            no_reparse(item)
            if item.is_symlink():
                raise RuntimeError(f"save preservation refuses a symlink: {item}")
            if item.is_dir():
                directories.append(item.relative_to(path).as_posix())
            elif item.is_file():
                files.append({
                    "path": item.relative_to(path).as_posix(),
                    "size": item.stat().st_size,
                    "sha256": _file_sha256(item),
                })
        kind = "directory"
    else:
        raise RuntimeError(f"unsupported save path kind: {path}")
    canonical = json.dumps({"directories": directories, "files": files},
                           ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True).encode("utf-8")
    return {"kind": kind, "directories": directories, "files": files,
            "sha256": hashlib.sha256(canonical).hexdigest()}


def _copy(source: Path, destination: Path, kind: str) -> None:
    if kind == "directory":
        shutil.copytree(source, destination)
    elif kind == "file":
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    else:
        raise ValueError(f"cannot copy save kind {kind!r}")


def _remove(path: Path, kind: str) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise RuntimeError(f"save preservation refuses to remove symlink: {path}")
    if kind == "directory":
        shutil.rmtree(path)
    else:
        path.unlink()


def _release_lock(state: dict, journal: Path) -> None:
    lock = Path(state["lock"])
    if not lock.is_file():
        return
    owner = lock.read_text(encoding="utf-8")
    try:
        owned = _canonical(Path(owner)) == _canonical(journal)
    except (OSError, ValueError):
        return
    if owned:
        lock.unlink()


def restore(journal: Path) -> dict:
    """Recover an interrupted or normally completed save-preservation session."""
    journal = Path(journal).resolve()
    state = json.loads(journal.read_text(encoding="utf-8"))
    from coding4game_gui.benchmark.windows import native_profile_io as native
    if state.get('native_io') and not native._IN_WORKER:
        return native.native_call('restore_save_session', {'journal': str(journal)})
    root = Path(state["source"])
    backup = Path(state["backup"])
    generated = Path(state["generated"])
    retained = Path(state["retained"])
    temporary = Path(state["restore_temporary"])
    for path in (root, backup, generated, retained, temporary):
        no_reparse(path)
    logical_root, logical_backup, logical_generated, logical_retained, logical_temporary = (
        _canonical(path) for path in (root, backup, generated, retained, temporary))
    journal_root = _canonical(journal.parent)
    if (logical_root.parent == logical_root or logical_backup.parent != journal_root
            or logical_generated.parent != journal_root
            or logical_backup == logical_generated or logical_root == journal_root
            or logical_root.is_relative_to(journal_root)
            or journal_root.is_relative_to(logical_root)
            or logical_retained.parent != logical_root.parent
            or not retained.name.startswith(root.name + '.c4g-generated-')
            or logical_temporary.parent != logical_root.parent
            or not temporary.name.startswith(root.name + '.c4g-restore-')):
        raise RuntimeError('unsafe save recovery paths')
    original = state["original_manifest"]
    original_exists = original["kind"] != "absent"
    save_state.kill_process(state["process"])

    if state["status"] == "captured":
        generated_manifest = tree_manifest(root)
        if generated_manifest["kind"] != "absent":
            if generated.exists():
                if tree_manifest(generated) != generated_manifest:
                    raise RuntimeError("generated save evidence does not match live save")
            else:
                _copy(root, generated, generated_manifest["kind"])
                if tree_manifest(generated) != generated_manifest:
                    raise RuntimeError("generated save evidence verification failed")
        state["generated_manifest"] = generated_manifest
        state["status"] = "generated_captured"
        _write_json(journal, state)

    if state["status"] == "generated_captured":
        if root.exists():
            if retained.exists():
                raise RuntimeError(f"both live and retained save paths exist: {root}; {retained}")
            root.rename(retained)
        state["status"] = "archived"
        _write_json(journal, state)

    if state["status"] == "archived":
        if original_exists:
            if tree_manifest(backup) != original:
                raise RuntimeError("original save backup verification failed")
            if temporary.exists():
                if tree_manifest(temporary) != original:
                    raise RuntimeError(f"stale restore temporary is invalid: {temporary}")
            else:
                _copy(backup, temporary, original["kind"])
            if tree_manifest(temporary) != original:
                raise RuntimeError("restored save staging verification failed")
            if root.exists():
                raise RuntimeError(f"save path was recreated during restoration: {root}")
            temporary.rename(root)
        elif root.exists():
            # Steam can recreate only its cloud-account marker after game exit.
            # Preserve those new bytes, reject all game/user data, and reconcile
            # once rather than repeatedly moving a changing directory.
            import re
            late = tree_manifest(root)
            if (late['kind'] != 'directory' or late.get('directories')
                    or [entry['path'] for entry in late['files']] != ['steam_autocloud.vdf']
                    or late['files'][0]['size'] > 4096):
                raise RuntimeError('save path was recreated with non-platform data')
            cloud = (root / 'steam_autocloud.vdf').read_text(encoding='utf-8')
            account = re.fullmatch(r'\s*"steam_autocloud\.vdf"\s*\{\s*"accountid"\s*"(\d+)"\s*\}\s*', cloud)
            if (account is None or not root.name.isdecimal()
                    or int(root.name) != 76561197960265728 + int(account[1])):
                raise RuntimeError('unexpected platform save-account metadata')
            late_path = journal.parent / 'platform-generated-save'
            no_reparse(late_path)
            if late_path.exists():
                if tree_manifest(late_path) != late:
                    raise RuntimeError('late platform metadata evidence changed')
            else:
                _copy(root, late_path, 'directory')
            if tree_manifest(late_path) != late or tree_manifest(root) != late:
                raise RuntimeError('platform metadata changed during preservation')
            state['late_platform_manifest'] = late
            state['late_platform_evidence'] = str(late_path)
            _write_json(journal, state)
            _remove(root, 'directory')
        if tree_manifest(root) != original:
            raise RuntimeError("player save restoration digest mismatch")
        state["restored_manifest"] = tree_manifest(root)
        state["status"] = "restored"
        _write_json(journal, state)

    if state["status"] == "restored":
        if retained.exists():
            generated_manifest = state.get("generated_manifest") or tree_manifest(retained)
            if generated_manifest["kind"] != "absent" and tree_manifest(generated) != generated_manifest:
                raise RuntimeError("cannot remove retained save: generated evidence is not verified")
            _remove(retained, generated_manifest["kind"])
        state["status"] = "restored_verified"
        _write_json(journal, state)

    if state["status"] != "restored_verified":
        raise RuntimeError(f"unknown save preservation status: {state['status']}")
    _release_lock(state, journal)
    return state


def begin_save_session(spec: dict, evidence: Path):
    """Capture the registered original before any benchmark save mutation."""
    startup = spec.get("startup") or {}
    if not startup.get("preserve_player_save", False):
        return None
    checkpoint = startup.get("golden_checkpoint")
    if not checkpoint:
        raise ValueError("preserve_player_save requires startup.golden_checkpoint")

    from coding4game_gui.benchmark.windows.games_config import load_game_config
    cfg = load_game_config(spec["driver_game"])
    resolved = save_state.resolve_for_run(
        spec["driver_game"], cfg.save_state, checkpoint)
    if resolved is None:
        raise ValueError("preserve_player_save requires a registered save path")
    root = resolved.restore_to.absolute()
    from coding4game_gui.benchmark.windows import native_profile_io as native
    if native.needs_native_io(root.parent):
        return native.native_call('begin_save_session', {
            'scenario': spec['runtime']['scenario'],
            'windows_input': spec['runtime'].get('windows_input'),
            'evidence': str(Path(evidence).absolute())})
    root = root.resolve()
    no_reparse(root)
    if root.parent == root or root.is_symlink():
        raise RuntimeError(f"unsafe player save preservation target: {root}")

    # Keep the recovery journal inside the run while allowing its nested save
    # files to exceed the legacy Windows MAX_PATH limit.
    evidence = _extended(Path(evidence).resolve())
    logical_evidence, logical_root = _canonical(evidence), _canonical(root)
    if (logical_evidence == logical_root or logical_evidence.is_relative_to(logical_root)
            or logical_root.is_relative_to(logical_evidence)):
        raise RuntimeError('save and evidence paths must be disjoint')
    evidence.mkdir(parents=True, exist_ok=False)
    journal = evidence / "save-session.json"
    lock = root.parent / f".c4g-{spec['driver_game']}-save.lock"
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(journal))
    except FileExistsError as exc:
        owner = lock.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(
            f"player save is owned by an active or interrupted run; inspect {lock}. "
            f"After stopping it, recover with python -m "
            f"coding4game_gui.benchmark.windows.save_session --recover {owner}") from exc

    state = None
    try:
        save_state.kill_process(cfg.process)
        original = tree_manifest(root)
        backup = evidence / "original-save"
        if original["kind"] != "absent":
            _copy(root, backup, original["kind"])
            if tree_manifest(backup) != original:
                raise RuntimeError("player save backup verification failed")
        suffix = uuid.uuid4().hex
        state = {
            "status": "captured",
            "scenario": spec.get("runtime", {}).get("scenario"),
            "driver_game": spec["driver_game"],
            "process": cfg.process,
            "source": str(root),
            "backup": str(backup),
            "generated": str(evidence / "generated-save"),
            "retained": str(root.with_name(root.name + ".c4g-generated-" + suffix)),
            "restore_temporary": str(root.with_name(root.name + ".c4g-restore-" + suffix)),
            "lock": str(lock),
            "original_manifest": original,
            "journal": str(journal),
        }
        _write_json(journal, state)
        return state
    except BaseException:
        if state is None:
            lock.unlink(missing_ok=True)
        raise


@contextmanager
def benchmark_save(spec: dict, evidence: Path):
    """Protect original player bytes through the entire outer invocation."""
    state = begin_save_session(spec, evidence)
    try:
        yield state
    finally:
        if state is not None:
            restored = restore(Path(state['journal']))
            state.clear()
            state.update(restored)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover", required=True, type=Path)
    restored = restore(parser.parse_args(argv).recover)
    print(json.dumps(restored, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
