---
name: writing-game-auto-launch
description: Calibrate a Windows game's direct executable launch and record the menu_skip path from cold start to the benchmark task.
---

# Calibrate Windows game startup

Read the [Windows game protocol](../../../docs/contracts/WINDOWS_GAME_PROTOCOL.md)
and [operator workflow](../../../docs/WORKFLOWS.md#record-and-verify-menu_skip).
Run real-game checks serially in the intended Windows session. Identify the
approved task start, resolved save destination or no-save cleanup, game
process, and capture backend before recording.

1. Discover an unattended local EXE and set its absolute path in
   `games/<driver>.yaml` under `auto_launch.launch_path`. An installed launcher
   that requires a platform or custom-argument dialog fails this gate. Use an
   unattended runtime EXE, and verify that its process matches the driver.

   ```powershell
   python scripts/calibration/test_auto_launch_dry.py --game <driver>
   ```

2. Verify the input-ready screen after the configured settle. Use
   `settle_seconds: 60.0` for new recordings and preserve the calibrated value
   for existing ones. Inspect the screenshot sequence in the printed
   `out/auto_nav_calibration/` directory. Auto-advancing splash screens belong
   in the settle interval. For DXcam, verify the game owns the foreground.

   ```powershell
   python scripts/calibration/test_auto_launch_calibrate.py --scenario <scenario>
   ```

3. Use **Record & calibrate** → **Record menu navigation** and pick the
   scenario, or preview the measured YAML with the recorder. The scenario
   determines the approved task-state preparation and window setup. Click the
   virtual pad through the entire title-to-task path, including both pads for
   co-op. Bind P1 first if the title's join screen assigns slots by press order.
   **SAVE** prints the steps in dry run. Re-record without `--dry-run` to replace
   the driver's `menu_skip`. Use measured inputs and waits.

   ```powershell
   python scripts/calibration/record_pad_to_yaml.py --game <driver> --scenario <scenario> --auto-launch --dry-run
   ```

4. Verify the same configured replay mode the formal scenario uses.
   `closed_loop` checks configured menu destinations. `open_loop` follows the
   recorded timing and requires calibration for that exact task start.
   Inspect `01_post_settle.png`, `02_after_menu_skip.png`, and `result.json`
   in the printed `out/auto_launch_full_test/` directory. Require the intended
   task and frozen handoff on repeated fresh starts.

   ```powershell
   python scripts/calibration/test_auto_launch_full.py --scenario <scenario>
   ```

Review the driver diff. Keep the virtual pads alive from menu replay through
handoff so local player bindings survive. When replay diverges, verify the
save, window, and post-settle screen before recording again.
