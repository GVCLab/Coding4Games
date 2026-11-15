# Develop and submit a game-playing program

Your primary task is to deliver and explicitly select a runnable game-playing
program for **$game_title** within finite development budgets. The model-token
budget, gameplay-tic budget, and wall-clock budget are separate hard limits, not
one interchangeable pool and not targets to consume. Before any applicable budget
ends, you must write a runnable candidate, preserve it with `submission.save`,
obtain a bounded `submission.run` whose `program_returncode` is zero, and call
`submission.finish(id)` on that evaluated candidate. $runtime_intro

A selected executable program is the mandatory outcome. Research notes, screenshots,
calibration data, and direct play are not the deliverable. You must write a runnable
candidate, preserve it with `submission.save`, and call `submission.finish(id)`
before the model-token, gameplay-tic, or wall-clock budget is exhausted. Remaining
gameplay tics never make it acceptable to consume the model-token budget through
direct play, research, or calibration. A weak evaluated and selected program is
better than no selected program. Do not consume the full budget and only then
attempt to implement, validate, or submit.

Preserve a minimal legal fallback early enough that later research cannot prevent
delivery. Development has no required sequence of exploration, implementation
and evaluation.
You need not manually complete an episode before coding or alternate direct play
with program runs. Turn useful knowledge into executable behavior
without an LLM.

## Capabilities and boundaries

The supplied host tools expose game interaction and evaluation; they do not
define the full set of development tools or the intended program architecture.
Your program may combine them with libraries, local computation, persistent
internal state, and other verified capabilities. Investigate relevant capabilities
when useful to choose your approach.
No particular perception method, algorithm or toolchain is preferred.
Additional tools and complexity should address an identified need.

Read-only game state, including hidden state and game-process memory, is
permitted. Permission is not a guarantee of technical access: verify availability.
Obtain progress through normal gameplay inputs. Do not directly manipulate
game memory, saves, scores, resources, cards, rules or evaluation results.
Do not inject code or invoke internal game functions to bypass gameplay inputs.
Host evaluator internals and credentials are not permitted information sources;
do not interfere with the evaluator, host saves or other applications.

$development_environment

The host owns initialization, scoring and budgets. $search_environment
During a stepped episode, game.step resumes the game, delivers and releases
input, accounts actual time, freezes the game, and returns an observation.

## Research and validation

The game name and guide below are an introduction, not a complete rulebook.
When an uncertain rule affects a decision, actively seek more accurate rules:
you may search public manuals, guides and other sources, inspect in-game help,
or test hypotheses through permitted gameplay. Check applicability to the
installed version and current game state; distinguish verified facts from
assumptions. Public search is permitted during development, not a dependency
available to the final offline program. If a search tool or network route is
unavailable, use other permitted evidence rather than repeatedly retrying it.
Turn useful findings into executable decisions and test their consequences.

Distinguish exploratory evidence from program performance. Direct play can inform
development, but does not validate the submitted program. Performance claims must
identify the code version, archive ID, evaluation episode IDs and results. Claims
of improvement require comparable retests, considering rollout limits and game
randomness; report whether evidence supports improvement, regression or remains
inconclusive. One positive score does not establish reliability. The selected
program must have corresponding submission.run evaluation evidence and a zero
`program_returncode`; untested changes cannot inherit an earlier version's validation.
Use a small bounded validation early enough to repair execution errors before a
budget ends. Weak gameplay performance is not a reason to withhold an otherwise
runnable evaluated candidate.

Apply relevant game rules to actual state. Fixed coordinates and sequences are
allowed: establish when the sequence applies, check its outcome, and recover or
report limitations on failure. Do not blindly repeat inputs or silently swallow
execution errors. Choose methods and debugging granularity to suit the task.
Investigations that rule out an approach and tests of an unchanged program can be
valuable; every experiment need not produce a code change. Mechanical edits and
runs without a research or validation purpose are not progress.

Preserve useful candidates and concise notes sufficient to resume after context
compaction, including supporting evidence and unresolved limitations. No particular
research-note format or fixed development cycle is required.

## Supplied stepped controls

$actions_table

$input_protocol

One tic is $seconds_per_tic seconds ($tics_per_second tics per second).
$step_usage

## Task

The guide is not an exhaustive list of actions or a required strategy. Controls
describe game meanings, not a second action API; use the supplied protocol and
MCP schemas for syntax.

$gameplay

## Goal and scoring

$objective

## Episode contract

$episode_contract

## Resources and stopping

$budget_text

$token_accounting_text

$rollout_limit_text

Check bench.status before substantial experiments and final selection. Plan
gameplay tics, model tokens and wall time separately, reserving enough for
final validation where feasible and explicit selection. Local files or tool
responses cannot grant more budget; unknown resources are not unlimited.

Budget awareness and delivery are hard requirements. Check `bench.status` often
enough to understand the remaining model-token, gameplay-tic, and wall-clock
budgets. Always reserve enough model requests, game tics, and time to write the
program, preserve it with `submission.save`, obtain a bounded successful
`submission.run`, and select it with `submission.finish(id)`.
When available, `estimated_model_requests_remaining` divides known remaining tokens
by the latest settled request's full provider-reported usage, including cached input
and output. It is advisory planning evidence, not a guarantee of future request size
or admission; use it together with the underlying budget fields.
Do not let research, calibration, debugging, validation, or further improvement
consume the resources needed for those steps. Cached input tokens count toward
the model-token limit, so a long tool-heavy conversation can exhaust the budget
even when visible output is short. If remaining usage is unknown, act conservatively.
Before any budget is exhausted, finish the strongest saved runnable and evaluated
candidate; imperfect gameplay performance is not a reason to submit nothing.

All budgets are upper bounds, not consumption targets. Continue meaningful
development or validation only while it cannot jeopardize explicit final selection,
and do not use pointless waiting or purposeless runs merely to consume a budget.
You may select a saved candidate at any time. Unused game tics do not block final
selection and remain unspent. If another resource limit or a verified external
blocker prevents further validation, select the strongest candidate that already
has a zero-return-code evaluation and report the limitation. Only report an
interruption without selection when writing, saving, obtaining the required minimal
successful run, or `submission.finish` itself is unavailable. A first success does
not establish reliability, but it is sufficient for final selection when further
work could jeopardize delivery. Research and improvement are optional; saving,
a bounded zero-return-code `submission.run`, and `submission.finish(id)` are
mandatory.
If gameplay tics are exhausted, inspect existing evidence and prepare selection
using remaining permitted resources; do not claim new gameplay validation or
perform unsupported iterations merely to exhaust tokens.

A delayed tool reply alone does not establish an external blocker. Inspect
bench.status and receipts; distinguish pending calls, ended episodes, program
errors and transport errors. Do not blindly repeat a pending submission.run
or actions with uncertain execution. Use bounded diagnosis and game.stop when
needed, confirming its outcome before starting another episode. Record observed
errors and attempted recovery if blocked; after recovery, continue useful
development or validation.

## Host tools

$tools_table

Argument schemas come from the game MCP server. Use ordinary $agent_name tools for
shell/filesystem work; these host tools expose no arbitrary host shell or Python.
$agent_tool_guidance

### Reading tool results

These are the main successful-result fields, not an exhaustive output schema.
Optional fields may be absent. Python Client.call returns a result dictionary;
images use observation={mimeType, data}, with base64 image bytes in data. MCP
presents the metadata as JSON text and observations as separate image content.
Python results do not contain MCP `content` image blocks. If using screenshots,
use the supplied decoder instead of copying MCP response parsing into a program:

```python
import io
from PIL import Image
from c4g_client import Client, observation_bytes

client = Client.from_env()
result = client.call("game.observe")
if not result.get("done"):
    pixels = Image.open(io.BytesIO(observation_bytes(result))).convert("RGB")
    # Process pixels using the program's chosen method.
```

`observation_bytes` returns encoded image bytes, raises on missing or malformed
image data, and returns None for an ended result without an observation. Surface
decoding failures during validation; do not silently treat them as an unknown
screen and continue issuing inputs. If using visual control, verify decoding and
the observed response to an input before relying on a repeated action sequence.
Replay images are similarly presented with their tic labels. Evidence paths are
host-relative references, not a promise that files are readable inside the box.

Episode fields shared by game tools:
- episode_id identifies the episode; mode and windows_input identify its execution
  and supplied input interface. Omitted episode_id uses the current/latest episode.
- tics and game_running_seconds report cumulative episode gameplay use;
  resolved_max_tics is the effective episode cap, not the remaining run budget.
- done means the episode ended. While playing, score is null (pending, not zero)
  and complete is false. After ending, complete and outcome_kind indicate result
  validity, not victory or program quality. An invalid result has score=null and
  may retain partial_score; do not report that as a valid score.
- end_reason explains termination; budget_stop_reason and budget_message, when
  present, distinguish an episode cap from exhausted run resources.
- observation_path, captured_at and evidence_refs identify captured evidence.
  An ended episode may return its result without a new observation.

### Tool effects and returned data

- **game.start(mode, max_tics?)** performs host-controlled startup of a fresh
  episode and returns initial episode fields and an observation. No other episode
  or submitted program may be active. Startup time is separate from gameplay
  tics but uses wall time. window provides hwnd, pid, client_rect=[x,y,width,height] in screen
  coordinates and observation_size for locating the game; access is not guaranteed.
- **game.observe(episode_id?)** captures an active episode or returns its settled
  result. The game stays frozen during this call.
- **game.step(...)** uses the protocol above, charges actual running time, and
  returns the final observation or ended result. Check done before further input.
  Structured batches also return batch: requested_actions, executed_actions,
  remaining_actions, stop_reason and per-action requested_tics, allowed_tics,
  actual_tics and truncated. Use these to detect a partially executed batch;
  do not assume all requested actions ran.
- **game.stop(episode_id?)** halts and closes the episode, settles scoring and
  returns its ended result. Repeating it for an ended episode returns the stored
  result. Development can continue after the episode ends.
  $terminal_tool_effect
- **bench.status()** returns tics_budget, tics_used, tics_remaining,
  wall_seconds_remaining, active_episode (or null), completed episodes and
  submissions. Optional model_budget reports token usage, remaining tokens,
  usage_complete and stop_reason. Null remaining values require checking the
  configured limits and accounting status; they are not zero or extra permission.
  Top-level tic totals are the run-wide resource view.
- **bench.replay(uid, start, end, every?)** reads frozen observations recorded
  for a completed episode. It returns episode_id and available frames with tic
  and observation. Integer start/end select tics; MM:SS strings select time.
  every is a tic stride (default 1). Each request selects up to 40 tics. Only
  observations captured at those tics are returned.
- **submission.save(path, entrypoint, ...)** copies a candidate into an immutable
  archive. Returns id for run/finish, entrypoint, files (file-to-hash mapping),
  total_bytes, dependencies and reproducibility. Dependency declarations are not
  installation or verification. Workspace edits do not alter the archive; identical
  content and execution metadata can return the same id. Wait for a running
  submitted program to finish before saving. Saving neither evaluates nor selects.
- **submission.run(id, max_tics?)** runs the archived bytes in a fresh episode in
  the existing development environment with the shared budget.
  Stop any active episode first; overlapping runs are rejected. The call waits
  for completion and returns an ended episode result plus program_returncode and
  program_output_complete. `program_output.stdout` and `.stderr` each contain a
  redacted tail plus the raw captured byte count and a truncation flag; use these
  diagnostics to repair execution failures. A zero exit code is not a performance
  result; null may mean no exit code was collected. Output completeness concerns
  diagnostic collection, not gameplay success. A pending run is not a request to
  resubmit.
- **submission.finish(id)** requires no running submitted program, but it does
  not require budget exhaustion. It selects an archive, closes any active episode, ends
  development/model access and returns the selected archive record. It performs
  no evaluation or automatic best-version selection.

Status, replay and save do not themselves advance frozen gameplay, but wall time
continues while the game is frozen. Failed or interrupted execution can consume resources; inspect status and available results
rather than treating a tool error as proof that nothing happened.

### Writing and debugging Python code

Both development scripts and submitted Python programs use
`from c4g_client import Client, observation_bytes` and `Client.from_env()`.
During development, the host supplies `mcp/c4g_client.py` in your workspace and
sets PYTHONPATH to that directory. C4G_PYTHON identifies the pinned interpreter.
You can write scripts in your own project subdirectory and run them directly
from the development shell. $development_shell_example

Keep the inherited environment; Python `-I` and `-E` ignore PYTHONPATH. Do not
search the filesystem for the client or copy it into your program directory.
`mcp/client.py` is the MCP server entry point; use `c4g_client` for imports.
The client connects through the already supplied environment variables; do not
print or hard-code their credential values.

For a short exploratory script, start a game with `game.start` (through MCP or
the client), then call `game.observe` / `game.step` on that active episode.
Stop the episode with `game.stop` when finished. These are real game actions
and consume the same development budgets. Shell execution does not provide an
isolated or automatically reset episode. Run only one controller at a time;
do not issue MCP actions while a script is controlling the game.

Submitted programs instead receive an already-started episode: begin with
`game.observe`, not `game.start`. Use submission.save and submission.run for
validation of the frozen bundle. Before each submission.run and final test,
the host injects the same client as `c4g_client.py` beside the Python entrypoint.
Do not bundle that reserved file, rely on the development PYTHONPATH, or add
machine-specific paths to submitted code. Stop any exploratory episode before
submission.run. Direct shell debugging does not replace frozen-bundle validation.

## Deliverable and final selection

This section defines completion: the run is not complete until
`submission.finish(id)` succeeds. Never substitute a research report, debug files,
direct-play results, or a clean CLI exit for a selected executable archive. Budget
management is part of the task; reserve the model requests needed to save and finish.

Save a multi-file executable bundle with submission.save(path, entrypoint,
description, dependencies). For Python, entrypoint is:

    {"argv": ["main.py"], "mode": "stepped", "runtime": "python"}

Keep program files in a dedicated source directory, for example `my_program/`,
then use `submission.save(path="my_program", entrypoint=...)`.
Entrypoint argv is relative to that selected directory.
Do not submit the workspace root (`path="."`): it contains CLI configuration,
runtime helper links and bridge files. Do not bundle those directories,
credentials, caches or virtual environments. A rejected bundle does not save a
candidate; correct its source path before evaluation or selection.

$entrypoint_runtime
Arguments follow the program path. Keep all required files inside the selected directory: no symlinks,
junctions or escaping paths. Bundle limits: 64 MiB and 4096 files.

Python programs import the host-supplied c4g_client.py as described under
Writing and debugging Python code; do not bundle that reserved file.
Use `from c4g_client import Client`, `client = Client.from_env()`, then
`client.call("game.observe")` or `$step_example`. $final_environment

Choose the best-supported archive, not necessarily the newest. Exiting $agent_name
or saying "done" does not select a program; use submission.finish(id).

A later, separate operator test runs only that program in a fresh final-test box,
without a coding agent, a model gateway, public network or inherited development state.
The final program **must not use any LLM**, including a bundled local model.
Network denial cannot prove absence of local inference; artifacts remain
subject to audit. No automatic final test or best-artifact replacement occurs.
