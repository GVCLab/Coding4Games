"""Host-owned, recoverable preferences for a complete benchmark invocation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from coding4game_gui.benchmark.windows import game_settings
from coding4game_gui.benchmark.windows import save_state


def _save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _release_lock(state, journal):
    lock = Path(state['lock'])
    if lock.exists() and lock.read_text(encoding='utf-8') == str(journal.resolve()):
        lock.unlink()


def restore(journal: Path):
    """Preserve benchmark output, then put the original directory back."""
    state = json.loads(journal.read_text(encoding="utf-8"))
    if state['status'] == 'restored':
        _release_lock(state, journal)
        return
    root = Path(state['resolved_directory'])
    backup = Path(state['backup'])
    generated = Path(state['generated'])
    if root.name != 'Balatro' or root.is_symlink():
        raise RuntimeError(f"unsafe settings recovery target: {root}")
    save_state.kill_process(state['process'])
    # A persisted phase lets recovery resume after interruption between moves.
    if state['status'] == 'active':
        if root.exists() and not generated.exists():
            root.rename(generated)
        state['status'] = 'archived'
        _save(journal, state)
    if state['status'] == 'archived':
        if state['original_exists']:
            if not backup.is_dir():
                raise RuntimeError(f"original settings backup is missing: {backup}")
            shutil.copytree(backup, root, dirs_exist_ok=True)
        state['status'] = 'restored'
        _save(journal, state)
    _release_lock(state, journal)


@contextmanager
def benchmark_settings(spec: dict, evidence: Path):
    """Prepare before inference and restore after the game session closes.

    Only registered templates are accepted. An interrupted invocation leaves a
    journal and blocks the next one until explicit host recovery completes.
    """
    template = (spec.get('startup') or {}).get('settings_template')
    if template is None:
        yield None
        return
    if template != 'balatro-v1' or spec['driver_game'] != 'balatro':
        raise ValueError(f"unsupported startup settings template: {template}")
    from coding4game_gui.benchmark.windows.games_config import load_game_config
    cfg = load_game_config(spec['driver_game'])
    target = game_settings.balatro_settings_target()
    root = target.parent.resolve()
    lock = root.parent / '.c4g-balatro-settings.lock'
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    journal = evidence / 'settings-session.json'
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open('x', encoding='utf-8') as stream:
            stream.write(str(journal))
    except FileExistsError as exc:
        raise RuntimeError(
            f"Balatro settings are owned by an active or interrupted run; "
            f"inspect {lock}. After stopping that run, recover its journal with "
            "python -m coding4game_gui.benchmark.windows.settings_session --recover JOURNAL") from exc
    state = None
    try:
        save_state.kill_process(cfg.process)
        if root.is_symlink() or root.name != 'Balatro':
            raise RuntimeError(f"unsafe settings directory: {root}")
        original = root.exists()
        backup = evidence / 'original-balatro'
        if original:
            shutil.copytree(root, backup)
        # Keep the rename on the same volume; never discard generated saves.
        generated = root.with_name('Balatro.c4g-generated-' + uuid.uuid4().hex)
        state = dict(status='active', template=template, process=cfg.process,
                     logical_target=str(target), resolved_directory=str(root),
                     original_exists=original, backup=str(backup),
                     generated=str(generated), lock=str(lock))
        _save(journal, state)
        executable = Path(cfg.auto_launch.launch_path)
        state['game_executable'] = str(executable)
        with executable.open('rb') as stream:
            state['game_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        root.mkdir(parents=True, exist_ok=True)
        game_settings._write(target, game_settings.balatro_template())
        setup = spec['scorer']['window_setup']
        game_settings.force_balatro_window(tuple(setup['size']), setup['mode'])
        state['template_sha256'] = hashlib.sha256(
            game_settings.balatro_template().encode()).hexdigest()
        state['prepared_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        _save(journal, state)
        yield state
    finally:
        if state is not None:
            restore(journal)
            state.update(json.loads(journal.read_text(encoding='utf-8')))
        else:
            lock.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', required=True, type=Path)
    restore(parser.parse_args().recover)
