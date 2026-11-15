# Run on a new Windows machine

The [README](../README.md#quick-start) lists the steps for your first benchmark.
Use this guide to set up the Windows host, choose a runtime, and configure a game and model.

## 1. Prepare the host

Use an unlocked Windows x64 desktop and install Python **3.11 x64**. Install
ViGEmBus for virtual gamepad input. OCR tasks require a compatible NVIDIA GPU
and driver. Each game guide lists the supported build, launcher, saves, and
any additional dependencies. [Python installer](https://www.python.org/downloads/release/python-3119/#files) ·
[ViGEmBus releases](https://github.com/nefarius/ViGEmBus/releases) · [Game catalog](SUPPORTED_GAMES.md)

### Download the source

Extract the source ZIP or clone the branch with Git for Windows. [Git for Windows](https://git-scm.com/install/windows)

```powershell
git clone --depth 1 https://github.com/GVCLab/Coding4Games.git
cd Coding4Games
```

### Enable WSL 2 on Windows

On a machine without WSL, run this in **Administrator PowerShell**, then restart if prompted. [Microsoft WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install)

```powershell
wsl --install --no-distribution
```

Run `wsl --update` to get support for installing a named distribution. The
project setup selects WSL version 2 for its own distribution. [WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)

### Install the Windows dependencies

Open regular PowerShell in the checkout and start host setup.

```powershell
./scripts/start.ps1 -Setup
```

Review the preview, enter `y`, then choose **Quit** when the menu opens.

If you need to select the installer interpreter, use
`./scripts/start.ps1 -Setup -Python C:/Python311/python.exe` with your actual
Python path. The entry previews the installation and asks before running it.
Add `-Preview` to print the plan and exit. Setup creates `.venv`,
installs this checkout and its host dependencies, runs `pip check`, and copies
missing local configuration files. Existing environments and settings are never
replaced. A failed installation remains available for diagnosis. Setup downloads
packages but does not provision WSL, install drivers, change saves, launch a game
or call a model. Use `-Dev` to include software-test dependencies.

Python dependencies are declared in [pyproject.toml](../pyproject.toml).
Setup downloads the official [CUDA 11.8 PaddlePaddle wheel](https://www.paddlepaddle.org.cn/packages/stable/cu118/paddlepaddle-gpu/) for Windows.
Install ViGEmBus separately. When installing manually, set
`VGAMEPAD_SKIP_VIGEMBUS_INSTALL=true` in the shell to keep its driver installer
out of Python setup. Include `[dev]` for software-test dependencies.

If you use Conda, create the environment with `conda create -n coding4game_gui python=3.11 pip tk`
and activate it with `conda activate coding4game_gui` before running these commands.

```powershell
$env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = 'true'
python -m pip install -e ".[dev]"
python -c "from coding4game_gui.operator_config import initialize; print(initialize())"
```

Open the menu with an existing environment via
`./scripts/start.ps1 -Python C:/your/environment/python.exe`.

## 2. Configure host-only settings

| File | What to set |
|---|---|
| `launcher.local.json` | Generated WSL `profile`, provider `credentials` file, optional `output_root`, and TUI preferences. Relative paths resolve from the project root. [Template](examples/launcher.json) |
| `litellm.config.yaml` | Provider endpoint, model ID, and alias. Keep the key reference as `os.environ/C4G_MODEL_API_KEY` and set that variable on the host. [Template](examples/provider.yaml) |
| `games.local.json` | Local game executable and verified save locations, keyed by driver ID. [Template](examples/games.json) |

All three are ignored by Git and excluded from prepared source releases.
The operator menu reads the model and development budgets from its saved
settings. Batch launchers accept explicit parameters.

### Game paths and saves

For a Steam build, sign in to the Windows client, launch the game once, and
quit the game with Steam still signed in. Use **Manage → Browse local files**
to find the game executable. Check the linked driver for the expected launch
path. Some drivers launch via `steam.exe`.

Use the [driver ID](SUPPORTED_GAMES.md) as the key in `games.local.json`.
Replace the example paths with the executable and save directory on your host.
Include `save_state` only if the driver declares it and you need a local
restore directory.

```json
{
  "DRIVER_ID": {
    "auto_launch": {"launch_path": "C:/path/to/launch.exe"},
    "save_state": {"restore_to": "C:/path/to/save-directory"}
  }
}
```

Path overrides preserve the registered actions, menu recordings, scoring,
timing, and checkpoint identity. Steam installations can also be relocated
through the existing exact-suffix library lookup. For account-bound saves, verify
the local account, world, and starting state before restoring a checkpoint.

### Model provider

For the default Claude Code runtime, put a Claude API model you can access
in `litellm.config.yaml`. Keep its alias in sync with `launcher.local.json`.
[Claude API key setup](https://platform.claude.com/docs/en/get-started#get-your-api-key)

```yaml
model_list:
  - model_name: my-model
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: https://api.anthropic.com
      api_key: os.environ/C4G_MODEL_API_KEY
```

Enter the key in the PowerShell session that will open the operator menu.

```powershell
$env:C4G_MODEL_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Claude API key' -AsSecureString)).Password
```

## 3. Create or select a WSL2 profile

After Windows setup, run this from the checkout in regular PowerShell.

```powershell
./scripts/start.ps1 -SetupWsl2
```

Review the preview and confirm. Setup downloads the pinned Linux x64 Claude
Code binary from Anthropic and checks its SHA-256 before installing it. It
provisions `C4G-WSL2-Base-LOCAL`, exports its Debian baseline, and sets the
generated profile in `launcher.local.json` for a new checkout. Use `-Preview`
to see the plan. [Claude Code release verification](https://code.claude.com/docs/en/setup#binary-integrity-and-code-signing)

### Existing profiles and Codex

To reuse a profile, set `profile` in `launcher.local.json` to its path.
The setup command uses that selection when present. To add Codex, use
`scripts/setup_wsl2_runtime.py` with `--base-profile`, the official
`--codex-package`, its SHA-256, and exact version to create a **new** baseline.
Pass `--codex-package-sha256` and `--codex-version` for the exact official
Linux x64 platform package. Select a new `--root` and a temporary `--distro`
with the prefix `C4G-WSL2-Build-`. Review the plan before adding `--execute`.
The setup prints the new profile path. Select it in `launcher.local.json`
and configure a matching Codex model route. Keep personal CLI homes on the host.

### Local CLI binary and interrupted setup

Supply `--claude-source` to use an exact local copy of the pinned Linux x64
binary. Preview the plan with this command, then repeat with `--execute`.

```powershell
./.venv/Scripts/python.exe scripts/setup_wsl2_runtime.py --first-run --distro C4G-WSL2-Base-LOCAL --claude-source C:/path/to/claude
```

For an interrupted setup with its distribution already registered, use the same
root and distro with `--resume-setup`. Resume requires the setup ownership
marker to match the distribution. Use `wsl --list --verbose` to verify version 2.

For no-model checks of a provisioned profile, preview
`python scripts/acceptance/wsl2_runtime_canaries.py --profile PATH --out OUTPUT`.
Adding `--execute` creates disposable environments and records cleanup.
See [runtime verification](contracts/EVIDENCE_AND_ACCEPTANCE.md) and
[dependency reconstruction](wsl2_dependencies.md).

## 4. Check prerequisites without running anything

Choose **Host setup and diagnostics** → **Check host prerequisites** for the
host check. For a specific task and model route, replace `SCENARIO_ID` with the
[scenario ID](SUPPORTED_GAMES.md) and adjust `--agent` and `--model` to match
your configured profile and model alias.

```powershell
./.venv/Scripts/python.exe -m coding4game_gui.doctor --list-games
$scenario = 'SCENARIO_ID'
./.venv/Scripts/python.exe -m coding4game_gui.doctor --game $scenario --agent claude --model my-model
```

`doctor` reports missing packages, WSL command, profile, selected CLI, provider
route and game resources. `--json` produces a structured report;
`--verify-baseline` additionally hashes the potentially large baseline TAR.
Exit code 1 means at least one static prerequisite is blocked. It does not start
WSL clones, games, save restoration or model requests. Passing it does not verify
GPU execution, input delivery, calibrated startup or isolation.

## 5. Verify the game and frozen start

Install and log in to your game first. Find any required checkpoint ZIP in the
[game catalog](SUPPORTED_GAMES.md) and verify its task, account, and display requirements.
Before using a save-restoring command, back up and review its destination.
In the operator menu, choose **Record & calibrate → Run auto-launch checks**.
Select the driver, then **Full auto-launch** and the scenario. You can also run
the no-model check directly with the same scenario ID.

```powershell
$scenario = 'SCENARIO_ID'
./.venv/Scripts/python.exe scripts/calibration/test_auto_launch_full.py --scenario $scenario
```

**This check launches and closes the game and prepares its registered save.**
Compare `02_after_menu_skip.png` in the output directory with the starting state
described in the game guide. Verify ordinary input, freeze timing, scoring, and
repeated cold starts using the onboarding workflow.
Do not run native GUI tests or another game alongside it. A new machine has not
inherited the original host's real-game acceptance.

If the recorded path no longer reaches the task, capture the inputs from the
matching post-settle state. [Record and verify `menu_skip`](WORKFLOWS.md#record-and-verify-menu_skip)

## 6. Develop and test a player

Choose **Run experiment** to preview and run bounded model-assisted
development. Keep the development directory printed by the launcher. In
**Independent final tests**, select **Test a development run** and enter that
directory. Inside its `test/` folder, each repeat's `config.json` records
`result.score` and `result_valid`. The same menu fills missing repeats.
[Commands and expected outputs](WORKFLOWS.md)

Closed-run recovery uses `./scripts/launchers/run_code.ps1 -ResumeRun DIRECTORY` with the original
session and cumulative allowance. [Recovery limits](contracts/OPERATOR_WORKFLOWS.md#closed-run-recovery)

## Common blockers

| Symptom | Next action |
|---|---|
| PowerShell blocks launcher scripts | For a reviewed Git checkout, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` in the current PowerShell session if your machine policy permits. For a trusted source ZIP, unblock it in Windows file Properties before extracting. [PowerShell execution policies](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies) |
| Python version/dependency failure | Use a dedicated 3.11 x64 environment; inspect pip output. Preserve an incompatible existing `.venv`. |
| Profile missing or selected agent absent | Complete WSL provisioning or create a new extended baseline; update `launcher.local.json`. |
| Provider route rejected | Replace example model/HTTPS endpoint and set the referenced host environment variable. Doctor sends no request. |
| Missing checkpoint/account mismatch | Obtain or prepare the registered task normally and verify it; see the game catalog. |
| Missing timing/native components | Check architecture, upstream artifacts and security software. Do not bypass the registered timing protocol. |
| Capture/focus/start check failure | Keep the game visible and desktop unlocked; inspect retained evidence and local display settings. |

Current isolation qualification remains
`autonomous_benchmark_eligible=false`.
