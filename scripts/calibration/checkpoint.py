"""Create a named checkpoint zip from the game's current save dir.

Usage:
    python scripts/calibration/checkpoint.py --game brotato golden

Flow:
    1. Resolves the game's save dir — from games/<game>.yaml's
       save_state.restore_to if set, otherwise from a known-defaults registry
       (coding4game_gui/benchmark/windows/save_paths.py).
    2. Prompts you to play to your desired checkpoint and Save & Quit.
    3. Kills the game (releases file locks).
    4. Zips the save dir into snapshots/<game>_<name>.zip.

To replay this checkpoint later:
    Reference the saved checkpoint in the matching scenario's startup settings.
"""

import argparse
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coding4game_gui.benchmark.windows.games_config import load_game_config
from coding4game_gui.benchmark.windows.save_paths import default_save_path
from coding4game_gui.benchmark.windows.save_state import checkpoint_zip_path
from coding4game_gui.benchmark.windows.save_state import kill_process


parser = argparse.ArgumentParser(
    description="Snapshot the game's current save dir into a named checkpoint zip."
)
parser.add_argument("--game", required=True,
                    help="Game name (looks up games/<name>.yaml).")
parser.add_argument("name", nargs="?", default=None,
                    help="Checkpoint name. Becomes snapshots/<game>_<name>.zip. "
                         "Equivalent to --checkpoint NAME.")
parser.add_argument("--checkpoint", type=str, default=None,
                    help="Same as the positional name.")
parser.add_argument("--include", action="append", default=None,
                    help="Optional. Include only these subpaths under the save dir "
                         "(repeatable). Use this when the save directory contains "
                         "many worlds in one dir and you only want one. Default: zip "
                         "the whole save dir.")
parser.add_argument("--no-prompt", action="store_true",
                    help="Skip the 'play and press Enter' prompt; snapshot immediately.")
parser.add_argument("--keep-running", action="store_true",
                    help="Don't kill the game process before zipping. Risky — "
                         "files can be in an inconsistent state if the game is "
                         "still writing.")
args = parser.parse_args()

if args.checkpoint and args.name:
    parser.error("specify the checkpoint name once — either as positional `name` or as --checkpoint NAME, not both")
checkpoint_name = args.checkpoint or args.name
if not checkpoint_name:
    parser.error("checkpoint name required (positional, or --checkpoint NAME)")

cfg = load_game_config(args.game)

if cfg.save_state and cfg.save_state.restore_to:
    save_dir = cfg.save_state.restore_to
    save_dir_source = f"yaml save_state.restore_to"
else:
    try:
        save_dir = default_save_path(args.game)
    except (KeyError, FileNotFoundError) as exc:
        parser.error(str(exc))
    save_dir_source = "default registry"

if not save_dir.exists():
    parser.error(f"save dir does not exist: {save_dir}")

print(f"Save dir: {save_dir}  ({save_dir_source})")
print(f"Game process: {cfg.process}")

if not args.no_prompt:
    print()
    print(">>> Launch the game now and play to your checkpoint.")
    print(">>> Then Save and Quit cleanly so the game flushes the save to disk.")
    print(">>> Press Enter here when ready to snapshot.")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        print("aborted")
        sys.exit(1)

if not args.keep_running:
    n = kill_process(cfg.process)
    if n:
        print(f"Closed {n} {cfg.process} process(es) so the save files aren't locked. "
              "The game is ready for checkpoint capture.")
        time.sleep(1.0)

out = checkpoint_zip_path(args.game, checkpoint_name)
out.parent.mkdir(parents=True, exist_ok=True)

count = 0
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
    if args.include:
        for inc in args.include:
            src = (save_dir / inc).resolve()
            if not src.exists():
                parser.error(f"--include path not found: {src}")
            if src.is_dir():
                for p in src.rglob("*"):
                    if p.is_file():
                        z.write(p, p.relative_to(save_dir))
                        count += 1
            else:
                z.write(src, src.relative_to(save_dir))
                count += 1
    else:
        for p in save_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(save_dir))
                count += 1

size_mb = out.stat().st_size / (1024 * 1024)
print()
print(f"Wrote {out}  ({count} files, {size_mb:.1f} MB)")
print()
print("Reference this checkpoint in the matching scenario startup settings.")
