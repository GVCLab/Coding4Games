# Benchmark checkpoints

The [game catalog](../docs/SUPPORTED_GAMES.md) links to the ten scenario
checkpoints in this directory. Keep each ZIP at the registered path listed
for its game.

If you are using a source-only bundle built by `scripts/build_source_release.py`,
download the archives from the [GitHub snapshots directory](https://github.com/GVCLab/Coding4Games/tree/main/snapshots).
Check the game's build, account, and task state before restoring. Create a
matching checkpoint from your own save if the included archive requires a
different account.

Review local save destinations before any restoring/clearing command. Follow
registered preservation behavior and back up personal progress. Static doctor
checks never restore a checkpoint.

## Create a checkpoint from your own save

1. Follow the game's setup guide to prepare the account or world that leads to
   the registered task, then save and quit. Set `save_state.restore_to` for the
   correct driver and account or world in `games.local.json`.
2. From the repository root, run the checkpoint tool with the driver ID and
   checkpoint name. For example, to capture the Black Myth task save:

   ```powershell
   ./.venv/Scripts/python.exe scripts/calibration/checkpoint.py --game black_myth golden
   ```

   This writes `snapshots/black_myth_golden.zip`. Back up any existing ZIP with
   that name first. Archive only the files described in the game guide.
3. Run the [full startup check](../docs/WORKFLOWS.md#record-and-verify-menu_skip)
   and confirm that the game reaches the benchmark start.
