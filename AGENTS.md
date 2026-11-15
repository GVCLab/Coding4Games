# Project rules

## Purpose and current architecture

The canonical purpose document is docs/PROJECT_PURPOSE.zh.md. Keep confirmed
purpose, success criteria, boundaries, assumptions and priorities current during
design discussions; do not record guesses as approved conclusions. Flag drift
from the main line and recommend handling, time-boxing or parking the detail.
Feasibility, experimental validity, cost, safety and irreversible architecture
choices remain on the main line.

Use a real Linux Codex or Claude Code CLI for development in a persistent-for-run
WSL2 environment, and a separately invoked final test of the selected frozen bundle.
Deployment must not use LLM inference. The host owns startup, scoring, accounting,
immutable archives and upstream credentials. Development, independent tests and
the Direct baseline use the registered stepped action protocol.

WSL2 is the sole development/final-test backend, currently supporting Codex and Claude Code
and Python submissions. Preserve its independent final-test environment,
dependency reconstruction and documented resume limits. Do not restore the
retired Sandboxie backend or silently run unsupported agents on the host.

The old Code/Lang search, early coach/policy Sandboxie implementation and
VizDoom-specific execution paths are retired. Do not restore them
or compatibility forwarding packages. Preserve historical evidence, saves and
credentials; they are not cleanup targets.

## Authorization and continuous goal execution

For ordinary work, explain intended changes, affected files/subsystems,
verification and material side effects before implementation. User approval of
the proposed scope authorizes implementation, necessary fixes and verification;
do not ask again for each file, command or routine decision. Seek approval for
material expansion beyond that authorization.

When the user explicitly starts or invokes a goal, treat it as standing
authorization to pursue that goal through completion. Continue implementation,
debugging, tests and necessary real-token experiments without intermediate
approval requests. This includes relevant game launch/close, configured save
restoration, WSL2 run-clone creation/cleanup and use of selected paid model services.
Do not turn a goal into a sequence of permission questions or stop after planning.
An ordinary task or a discussion mentioning goals is not itself an active goal.

Respect user scope, model choices, token/tic/time/cost limits and stop instructions.
A goal does not erase configured budgets or authorize unlimited spending,
unrelated external actions, protocol changes outside its scope, or irreversible
destruction of unrelated data. Choose reasonable bounded runs using existing
configuration when limits are unspecified; report estimates and actual usage.
Never silently raise budgets or disable accounting to keep progressing.
If a required fact, exhausted explicit budget or external permission blocks work,
continue independent work and report the specific blocker; do not request routine
reapproval of actions already covered by the goal.

Outside an authorized goal, real-token/API/subscription experiments require
explicit authorization covering the run or batch. State the run and rough
cost/time before requesting missing authorization. Existing authorization persists
across turns. Mocked/no-LLM tests and read-only inspection need no paid approval.
Treat uncertain billing as billable.

These authorization rules supersede older project-document or skill requirements
for repeated implementation, onboarding or experiment approval within the same
scope. They do not bypass tool/platform permissions or remove runtime safeguards.
Keep the user informed of progress, experimental choices and material side effects
without converting updates into approval gates. Finish with actual changes,
verification, spending/side effects and remaining limitations.

## Experiment and security boundaries

Progress must come from normal gameplay inputs. Read-only game state, including
memory, is permitted but availability must be verified. Do not manipulate
state/save/score or inject internal calls to bypass gameplay. Development prompts
remain method-neutral; evaluator internals and upstream credentials stay host-only.
Do not copy operator credentials into the guest or expose secrets in reports/tests.
Prompts and network denial alone do not prove isolation or absence of local LLMs.

Preserve registered action, scoring and resource-accounting contracts. Scores stay
null during gameplay and settle after each episode. Do not silently restore visual
terminal/stillness watchdogs. Keep failed/interrupted work and consumed resources
auditable; do not equate clean exit with artifact submission. Fake tests and config
checks do not establish real-game/CLI/isolation acceptance. Report software tests
and real acceptance separately.

## Experiment launchers

Use the generic PowerShell launchers under `scripts/launchers/` when supplying
experiment commands. Compose them with shell loops as needed. Operators open
the menu with `scripts/start.ps1`. Keep individual model/game batches out of
the launcher source.

## Language

Use English for project-authored prompts, agent/skill instructions, tool and
configuration descriptions, runtime messages, code comments and docstrings.
Human-facing documents/reports may be Chinese; do not inject them into agent task
context. Preserve localized OCR/error literals, Unicode fixtures, user input and
raw third-party output as data. Do not translate historical evidence or game assets.
Language edits must not change budgets, retries, actions or scoring.

## Navigation and required reading

Read relevant contracts before changing or executing their subsystems, including
from tests or scripts. Contracts own detailed rules; directory guides route to them.

- Development, prompts, gateway or isolation: [contract](docs/contracts/DEVELOPMENT_AND_ISOLATION.md).
- Drivers, scenarios, input, startup, timing or scoring: [contract](docs/contracts/WINDOWS_GAME_PROTOCOL.md).
- Logs, accounting, reports or acceptance: [contract](docs/contracts/EVIDENCE_AND_ACCEPTANCE.md).
- TUI, launchers, interactive runs or calibration: [contract](docs/contracts/OPERATOR_WORKFLOWS.md).
- Use [add-new-game](.agents/skills/add-new-game/SKILL.md) for onboarding and
  [writing-game-auto-launch](.agents/skills/writing-game-auto-launch/SKILL.md) for menu recording.

Modules live under coding4game_gui/: benchmark/ (code, direct, scenarios, evidence,
and windows/ for the host game environment), wsl2/ (isolation and Linux runtime),
runtime_client/ (standalone bridge client and transport/bundle resources). See docs/architecture/PROJECT_LAYOUT.zh.md.
The benchmark CLI exposes develop, test, and direct.
Operator entry: scripts/start.ps1. TUI: scripts/game_tui.py.
WSL2 setup: scripts/setup_wsl2_runtime.py.
