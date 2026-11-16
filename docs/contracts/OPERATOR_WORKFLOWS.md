# Operator workflows

Run real-game acceptance and desktop-affecting software tests serially.
The full pytest suite includes recorder tests that create native Tk windows;
their foreground or topmost windows can obscure a DXCam client and correctly
fail its capture guard. Wait for those tests and their windows to exit before
launching the next real game. Offline file/image analysis may overlap only
when it creates no desktop windows or input. Do not attribute a test-created
overlay to operator interference, or count the interrupted episode as accepted.

Operator questions can create system toast banners that obscure desktop game
capture. Ask before live acceptance, and clear or suppress the coordinator's
own banners before continuing. Dismissing a banner never answers or approves
the pending question. Record original presentation settings and restore them
before leaving the workstation idle or after the experiment; keep notification
center delivery intact. Do not send new question notifications during a live
desktop-capture check or blame such a banner on the user.

Before a DXcam startup creates its renderer, the shared cold-start path moves
an outside pointer into the focused game client. This dismisses taskbar hover
previews such as Task View without clicks, keys, shell-setting changes or hidden
gameplay input. An already in-client pointer is preserved. This preparation is
shared by formal reset, Full verification and human recording and never runs
after policy handoff; remaining overlays still fail the capture guard.

Verify the intended host's game installation, account, task resources and
ordinary input through the registered cold start before paid development.
The [game guides](../SUPPORTED_GAMES.md) specify the required resources.

For drivers requiring xspeedhack, the onboarding preflight reads the game
executable's PE architecture and checks the matching installed injector and DLL.
For an explicit Steam app launch, it resolves the game executable through the
installed app manifest.
Missing, empty or unresolved resources block CONFIG READY. This check imports
no injector, executes no native code and does not establish attachment, file
safety or real-game timing acceptance.

Root AGENTS.md authorization rules apply, including standing goal authorization
for skill-guided work. Paths beginning with benchmark/ or wsl2/ are relative to coding4game_gui/.
Other paths are repository-relative.

## Closed-run recovery

`run_code.ps1 -ResumeRun ORIGINAL_RUN` previews continuation of a closed exported
WSL2 development. Add `-Execute` only within paid authorization. It inherits the
original model/profile, protocol and cumulative limits, and rejects new-run
overrides. The public Python entry is `develop --resume-run ORIGINAL_RUN` with
matching original arguments. Live owners or retained registered clones are
refused without being stopped; no automatic attachment or blank fallback occurs.
An archive, an original CLI session, and verifiable cumulative usage are
required. Hard crashes still marked running require reconciliation outside the
resume command. A legacy finite deadline needs its original start timestamp.
See the [development contract](DEVELOPMENT_AND_ISOLATION.md#closed-run-state)
for ownership, archive, and accounting checks.

After an explicit request to update a closed development's Claude Code CLI,
`setup_wsl2_runtime.py --upgrade-run ORIGINAL_RUN --claude-source LINUX_BINARY
--claude-version VERSION --claude-sha256 SHA256 --authorization-basis TEXT`
previews a run-bound update. Add `--execute` to preserve the prior configuration
and record the verified executable under the original profile lock. This setup
step launches no model or game. The generic resume launcher subsequently installs
that executable in each fresh development clone while retaining the original
profile/baseline, CLI session and cumulative resources. Independent final tests
keep their separate clean environment. Updating does not automatically resume a
paused paid experiment or establish that a provider error has been fixed.

A full-request differential diagnosis may justify an explicitly authorized,
run-bound `model_gateway_compatibility` record for an Anthropic route. The
`session_uuid` mode normalizes only a matching native `metadata.user_id` to the
existing official CLI session UUID. Preserve the prior configuration and bind
its hash, run and session in the host-only record. The generic `-ResumeRun`
workflow validates this record along with ordinary ownership, archive and
resource checks; it grants no new allowance or session. Do not turn on this
compatibility mode globally or replace native history to make an API accept it.

When the operator explicitly requires pausing after the first HTTP 400, record
`model_gateway_stop_on_http_400: true` in the preserved original development
configuration under its profile lock, with the prior configuration and explicit
authorization retained. The generic resume workflow validates this boolean and
enables immediate gateway revocation, preventing additional paid native fallback
requests after that response. Keep the original error and all accounting.

A targeted leading-cd replay repair uses a host-only
`model_gateway_tool_replay` record with format
`c4g-run-anthropic-tool-replay-v1`, `replay_mode: restore_leading_cd`, original
run/session, preserved source configuration/hash and explicit authorization.
Recovery validates it twice and seeds original Bash inputs from complete native
gateway responses. Keep the official CLI history and existing metadata repair
unchanged. Unknown or broader input differences are never guessed or restored.

### Run-bound authorizations

The host JSON grants bind to `run_dir`, `source_config_sha256` and a nonempty
`authorization_basis`. Supply the matching `kind` and fields below. Preview
validates the grant without launching a runtime or a model. The controller
rechecks it under the profile lock and preserves the prior configuration and
grant evidence in the recovery segment.

- `-RecoveryRouteAuthorizationFile` (`--recovery-route-authorization`) also
  binds `from_upstream_url`, `to_upstream_url`, `agent`, `model`,
  `provider_model`, and `upstream_api`. Kind
  `c4g-recovery-route-authorization-v1` permits an HTTPS hostname change at
  the same path and effective port. Kind
  `c4g-recovery-endpoint-authorization-v1` permits a same-origin HTTPS path
  change with a run-local `accepted_request_receipt` and
  `accepted_request_receipt_sha256` for the exact session, model and endpoint.
  Later recovery checks the recorded migration lineage.
- `-RecoveryBudgetNoticeFile` (`--recovery-budget-notice`) uses
  `c4g-recovery-budget-notice-v1` and a finite
  `minimum_development_fraction` in (0, 1]. Version
  `c4g-recovery-budget-notice-v2` also requires
  `require_registered_terminal_evaluation: true`. These fields add a fixed
  review reminder within the original task and allowances. Set
  `-MinimumDevelopmentFraction` directly for a new development.
- `-DevelopmentContinuationAuthorizationFile`
  (`--development-continuation-authorization`) uses
  `c4g-development-continuation-authorization-v1`,
  `selected_program_id`, and `selection_sha256` over the active
  `programs/final.json`. It authorizes continuation of that exact selected
  run within its original task and allowances. Recovery preserves the prior
  pointer, records human assistance, and requires a new finish.

## Execution confirmation

Develop/test and the Direct batch launcher preview by default and execute with
the explicit execution switch. Development and Direct also require paid-run
confirmation at their benchmark entry points. Existing authorization under the
root AGENTS.md covers these flags.

Manually invoking scripts/run_single_experiment.py authorizes that single paid
run immediately without execution flags or interactive confirmation. Its --preview
option never launches a run; it supplies required flags internally. Assistant
invocation follows root task/goal authorization rules. TUI confirmations remain.

## TUI responsibility boundary

`scripts/start.ps1` opens the operator TUI in `scripts/game_tui.py`. It offers
host setup, registered experiments, independent tests, recording/calibration,
and launch diagnostics. Setup, configured development, independent tests, and
Direct use the generic launchers in `scripts/launchers/`.
The menu offers execution after a successful preview. Its experiment
flow must preview the exact current runner command, resource limits, billing
backend, and rough time/cost implications, then require an explicit
default-deny confirmation before starting any real-token run.

The TUI must not scan installations, guess executables, or generate a new game
YAML skeleton. New-game onboarding is a human-in-the-loop workflow driven by
the `add-new-game` skill. Recording `menu_skip` edits a driver under
`games/*.yaml`; scorer calibration selects a registered Windows benchmark
scenario, then resolves its `driver_game` through that scenario.

The human scorer recorder stops through its printed `stop.request` file or a
native game exit; F12 remains a game/Steam screenshot shortcut. A recording
failure after the human handoff releases its input/capture resources without
requesting game termination. Partial evidence retains the error and a cleanup
receipt and does not count as acceptance. Startup failures and normal recording
completion retain the existing game cleanup policy.
Human sessions should use `--until-stopped`: this mode has no duration stop,
cannot be combined with `--max-seconds`, and does not terminate the game after
handoff even on normal recording completion. It publishes `status`, frame
count and the last captured time in atomic metadata snapshots every five
seconds. Announce readiness only after the validated task image and sustained
frame/event writes have been checked. Short automatic diagnostics are not
human recording handoffs.
During human calibration, a known foreground/occlusion capture guard pauses
frame collection and keeps the input owner alive. It logs capture-gap start/end
events and publishes `capture_status: waiting_for_visible_game`; collection
resumes only after the ordinary visibility guard passes. Gap frames are not
fabricated. Other capture errors retain the failure cleanup above. This does
not relax formal experiment capture guards or establish continuous evidence
across an observed gap.

## Development terminal status

Codex API errors are printed as timestamped, redacted single-line notices,
including CLI reconnect progress. Identical consecutive errors are suppressed;
a completed model turn reports recovery. Host recovery prints its wait interval
and any wait for outstanding host calls. Token breakdown summaries print at most
once per minute, except budget stop changes which print immediately. Full budget
states and CLI events remain in the run evidence; terminal throttling does not
change accounting, retry policy or tool execution.

## Batch launchers

`run_baseline.ps1 -Games SCENARIO -Models MODEL` previews the stepped Direct
baseline. The selected scenario supplies the input mode, and its registered
tic cap bounds `-TicBudget` for each episode. Model requests use the configured
host LiteLLM provider. Supply `-Execute` to launch the previewed matrix.
`run_code.ps1` is the development matrix entry; `test_code.ps1` tests selected
frozen bundles from a list of development directories.
The development launcher and single-run helper use the same scenario catalog.
Automatic input selection follows that catalog, and an explicit input choice
takes precedence. The matrix previews every job before any paid execution.
Desktop-based runs require the game to remain in the foreground, uncovered and
non-minimized. Capture checks report detected overlays; they do not disable
notifications or modify other applications. Native DPI geometry and real
repeated-start/timing acceptance remain required after backend changes.
`run_code.ps1 -TaskInstructionsFile FILE` appends a run-specific UTF-8 instruction
file of at most 64 KiB under `Additional operator instructions` in the generated
development task. Preview validates and reports only its byte count and SHA-256;
the exact text is snapshotted before runtime side effects and retained in
`task_prompt.md`. Omitting the option preserves the standard task unchanged.
`run_code.ps1` and the single-run helper default to WSL2 and Claude Code;
`-Agent codex` selects the Linux Codex CLI. The profile must contain that agent.
`-RuntimeBackend wsl2` remains an explicit label; other backend values are
rejected. `-Profile PROFILE` must identify a WSL2 baseline; Python submissions
are the current supported artifact type. `test_code.ps1` inherits the WSL2
profile from the source run and creates a separate clean environment. It rejects
historical or missing backend labels. `test_code_remaining.ps1` discovers only
completed WSL2 sources and counts only matching WSL2 repeats. Historical records
are preserved without reinterpretation. Static preview does not establish real
CLI/game/isolation acceptance.
Keep the generic batch launchers under `scripts/launchers/`.
New Code development can opt into `-StopOnHttp400` on `run_code.ps1`. The
single-run and benchmark CLIs carry `--stop-on-http-400`; the run records
`model_gateway_stop_on_http_400: true` and enables the existing gateway
revocation guard for the first upstream HTTP 400. It preserves failed evidence
and unknown usage while preventing paid CLI fallbacks. Omission retains the
existing default. Resume inherits the recorded policy and rejects an override.
Compose these entries in operator commands for a requested matrix; do not add
task-specific batch scripts. The single-run helper's optional `--out-dir`
refuses any existing directory.
The development and baseline launchers read `output_root` from
`launcher.local.json` or default to `out/experiments` in the checkout.
`-OutputRoot` supplies an explicit destination.
`test_code_remaining.ps1 -Execute` discovers completed developments outside
archives and baselines, and tops each up to three valid completed tests. Omit
`-Execute` to preview. Rerunning counts existing tests for the selected program
with matching scenario, input and configured budgets; interrupted or invalid
tests do not count. Existing evidence is preserved. The summary uses the earliest
three matching tests and prints their scores and mean. A null score counts as a
completed valid test but leaves the three-test mean unavailable. Failures stop
the batch with a partial summary; inspect the failure before rerunning.
Test results are grouped under each source development directory at
`test/<batch_id>_job_<index>/repeat_<index>_input-<mode>/`; each invocation uses a fresh batch
ID so previous test evidence is preserved.
The PowerShell experiment launchers preview by default and require `-Execute` to run. Code defaults to one
development per game/model, and baseline/test default to three episodes/repeats.
The Direct batch records
failed jobs and continues the remaining matrix, returning nonzero at the end
if any job failed. Operator interruption stops the batch. Failed scores remain
null and excluded; continuing the batch does not validate a failed episode.
