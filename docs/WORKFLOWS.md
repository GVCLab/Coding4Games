# Operate the game benchmark

Open the operator menu with `./scripts/start.ps1` from the source checkout.
Complete the [new-machine setup](QUICKSTART.md) and check the game's
[requirements](SUPPORTED_GAMES.md). A driver name identifies
`games/<driver>.yaml`. A scenario name selects the benchmark task, start
state, scoring and limits in `coding4game_gui/benchmark/scenarios/`.

| Task | Menu | Result |
|---|---|---|
| Prepare the Windows host | **Host setup and diagnostics** | Python dependencies and missing local configuration files |
| Record menu navigation | **Record & calibrate** | A `menu_skip` sequence in the driver YAML |
| Develop a player | **Run experiment** | A development directory with an explicitly selected frozen Python program on completion |
| Test a frozen player or fill missing repeats | **Independent final tests** | Independent episode results under the development directory |

## Record and verify `menu_skip`

`menu_skip` replays the measured startup inputs from the post-settle title
screen to the registered task. Record it when onboarding a driver or when
the recorded route diverges on the intended game build and checkpoint.

1. Verify the local EXE, display and task resources. Close other game
   sessions and back up the save destination if the scenario restores a
   checkpoint. The recorder's scenario-aware cold start performs the
   registered preparation and launches the game.
2. Choose **Record & calibrate** → **Record menu navigation** and select the
   scenario. The menu applies its start state and window setup and records
   into the associated driver. To preview the measured YAML, run the recorder
   with `--dry-run`.

   ```powershell
   python scripts/calibration/record_pad_to_yaml.py --game balatro --scenario balatro --auto-launch --dry-run
   ```

   Use the matching driver and scenario IDs from the game page. In the
   recorder, click the virtual pad to demonstrate the route and wait for
   transitions as you normally would. For local co-op, record both pads in
   one session. Stop recording at the intended starting state and choose
   **SAVE**. With `--dry-run`, SAVE prints the measured YAML without writing
   it. Repeat without `--dry-run` to record and replace the driver's
   `menu_skip`. Choose **CANCEL** to discard.
3. Review `git diff -- games/<driver>.yaml` and run the shared startup check
   with the **scenario** name. This restores the registered starting state
   where configured, launches and closes the game, and makes no model calls.

   ```powershell
   python scripts/calibration/test_auto_launch_full.py --scenario balatro
   ```

   Open `01_post_settle.png`, `02_after_menu_skip.png` and `result.json`
   under the printed `out/auto_launch_full_test/` directory. Verify the
   post-settle screen and the frozen task handoff. Confirm ordinary input,
   freeze timing and score evidence on repeated starts before using the
   configuration for an experiment.

**Record scorer calibration** in the same TUI menu replays the existing
navigation and collects frames while a human plays. Create the printed
`stop.request` file to end that recording. For a new game, use the
[onboarding skill](../.agents/skills/add-new-game/SKILL.md) and the
[Windows game contract](contracts/WINDOWS_GAME_PROTOCOL.md).

## Develop a Python player

Choose **Run experiment**, pick a scenario and select the Linux Codex or
Claude Code development mode. **Edit saved settings** sets the WSL2 profile,
model alias, input mode and budgets. **Preview development** prints the
resolved runner command and resource limits. Confirm to execute.

Automation uses the same launcher. Use explicit cumulative development limits.

```powershell
$development = @{
    Games = 'balatro'
    Agent = 'claude'
    Models = 'my-model'
    TicBudget = 1200
    TokenLimit = 30000
    WallSeconds = 600
}
./scripts/launchers/run_code.ps1 @development
```

The launcher previews the full batch. The scenario applies its own per-episode
cap within the cumulative tic budget. With `-WindowsInput auto` (the default),
the launcher uses the scenario's configured input mode. Review the profile,
provider, limits and output location. Confirm in the menu or add `-Execute`
in automation to launch the game, create the development environment and make
paid model requests. Reported input, including cached input, and output count
toward the cumulative token threshold. A response can take usage past it.

For an end-to-end smoke test, use a short tic budget and pass
`-TaskInstructionsFile` with a request to evaluate and select a minimal player
early. Allow for the model calls needed to write, run, and select the program
even when gameplay is short.

Keep the actual development directory printed by the launcher. Read
`config.json` for the resolved configuration and run status. A completed
development explicitly selects a frozen archive under `programs/<id>/` and
records its identity in `programs/final.json`. Candidate archives without a
final selection are still development evidence.

## Independently test the frozen player

Choose **Independent final tests** → **Test a development run** and enter the
directory containing `config.json` and `programs/final.json`. Set the repeat
count, review the preview and confirm. **Top up incomplete tests** scans the
configured experiment directory or an explicit directory and fills the missing
valid repeats for each completed development.

Automation accepts the same development directory.

```powershell
$runDir = 'C:/path/to/DEVELOPMENT_DIRECTORY'
./scripts/launchers/test_code.ps1 -RunDirs $runDir -Repeats 3
./scripts/launchers/test_code.ps1 -RunDirs $runDir -Repeats 3 -Execute
```

Preview checks the selected program and source configuration. Each executed
repeat creates a clean WSL2 environment, reconstructs the frozen dependencies,
prepares the same game task and runs the selected program without model
inference. It inherits the original scenario, input protocol, profile and
limits. Read each repeat's `config.json` and episode evidence under
`test/<batch_id>_job_<index>/repeat_<n>_input-<mode>/` in the source directory. A missing
official score remains null. Inspect failures and partial evidence before
comparing repeats.

## Batch runs and recovery

Pass multiple scenario IDs or model aliases to `run_code.ps1` as comma-separated
lists. The launcher previews the matrix, runs combinations serially on execution,
and stops the batch on failure. Use `-Repeats` for independent development runs.
The launchers read `launcher.local.json` and accept explicit path overrides.
`test_code_remaining.ps1 -Root DIRECTORY -Repeats 3` previews the pending repeats
for registered scenarios under a run root.

Use `run_code.ps1 -ResumeRun DIRECTORY` to preview a closed development's
continuation with its original session, profile, and cumulative budgets.
Add `-Execute` when authorized. [Recovery requirements](contracts/OPERATOR_WORKFLOWS.md#closed-run-recovery)

## Direct baseline

Choose **Run experiment** → **Direct baseline**. The model receives screenshots
and replies with timed actions. The host freezes the game between actions and
settles the score after the episode. The scenario supplies the default input
mode and caps each episode's game tics.

Preview the same workflow in PowerShell with a configured host provider alias.

```powershell
./scripts/launchers/run_baseline.ps1 -Games balatro -Models my-model -Episodes 3 -TicBudget 1000
```

Add `-Execute` for an authorized paid run. The preview shows the effective
per-episode limits and billing backend. Model requests use the host provider
configuration. Review each run's `config.json`, `episodes.jsonl`,
`llm_calls.jsonl`, and settlement evidence under `evidence/`.
