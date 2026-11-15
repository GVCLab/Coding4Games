# Development, prompts and isolation

Root AGENTS.md authorization rules apply, including standing goal authorization
for skill-guided work. Paths beginning with benchmark/ or wsl2/ are relative to coding4game_gui/.
Other paths are repository-relative.

GameSession startup retries runtime/OS failures up to three complete attempts
per start request. Each failed attempt is cleaned up and journaled with its own
episode UID, null score, error and attempt number before a fresh environment is
created. Existing launcher-internal retries remain bounded separately. Every
attempt rechecks the original deadline and remaining tic budget; retries do not
reset accounting or grant resources. Operator interrupts, invalid configuration,
and cleanup failures are not retried. Nonretryable exceptions after lifecycle
startup begins close the session and signal the same fatal startup path;
invalid tool arguments checked before lifecycle startup remain correctable.
After exhausted attempts, nonretryable lifecycle failures or unsafe cleanup,
development revokes model access with game_startup_failed; Codex stops without
asking the agent to spend the remaining gameplay budget. This is an infrastructure
failure, never successful completion or automatic final selection.

If controller boot or scorer/action wiring fails before the Windows environment
adapter is returned, the constructor runs the controller's registered shutdown
before propagating the original error. The caller cannot close an object it
never received. A shutdown exception becomes a distinct, nonretryable
`startup_cleanup_failed` construction error, so the session records the failed
episode and stops rather than launching another game over unsafe cleanup.
Successful construction adds no cleanup, focus, input or timing operations.
Software regression evidence for this path does not establish native focus
recovery or explain a prior foreground failure.

WSL2 is the sole development and independent final-test backend.
Development runs an explicitly selected official Linux Codex or Claude Code CLI
in one persistent-for-this-run clean clone of the hash-pinned baseline. A later
explicit test runs the selected frozen Python program in a different clean clone.
The retired Windows backend, its visible PowerShell debug shells and its
development-resume implementation are removed. The CLI rejects unsupported
agents and runtimes instead of falling back to the host.

This selection does not change the host-owned game lifecycle, registered
action/scoring contracts, accounting, submission identity or credential boundary.
The host retains upstream credentials; WSL receives scoped gateway/game leases.
Python submissions require a successful zero-return `submission.run` before
`submission.finish`. Closed, exported WSL2 developments can continue through
`develop --resume-run ORIGINAL_RUN` under the same original allowance and session.
Already selected programmes proceed to independent tests. Existing evidence is
preserved and never converted to a Linux artifact by changing its metadata.

An explicit human request can update the official Claude Code executable for a
closed, unselected development with `setup_wsl2_runtime.py --upgrade-run`. This
paid-request-free operation holds the original profile lock, refuses live owners
and registered run clones, preserves the prior configuration, and records the
exact Linux executable hash, version, original session and authorization. Recovery
revalidates that record and all original identity and resource fields, then installs
and verifies the pinned executable as root inside the fresh development clone
before any worker starts. The original baseline/profile, CLI history, workspace
archives, model route and cumulative allowances stay unchanged. Each later
recovery reapplies the recorded update; independent final tests never install it.
The setup operation and version checks do not establish model/API acceptance.
Managed Claude Code launches explicitly set
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The CLI still receives only the
scoped local gateway token; the actual upstream channel key remains host-only.

A later explicit human request may continue a prematurely selected development
through a host-only, configuration-and-selection-bound continuation grant. This
is an assisted continuation, not a new fresh experiment or a budget reset. Keep
the same official CLI session, workspace, candidates, unknown usage, original
limits and deadline. Under the profile lock, preserve the old configuration and
final pointer before withdrawing the active selection; a new explicit finish is
required. Never include independent test results or strategies in the generic
budget-clarification notice. Ordinary recovery still refuses selected runs.

Recovery rejects upstream route changes by default. An explicit host operator
grant may authorize an HTTPS hostname change while retaining the endpoint path
and effective port. A separate endpoint grant may instead change only the path
on the same HTTPS origin, after a complete accepted Anthropic request has been
verified for the original session and model. Its run-local receipt is hash-pinned;
an outer HTTP 200 without accepted generation is insufficient. Model, protocol,
proxy and all original allowances remain unchanged. The
grant is bound to the original run directory and full closed configuration hash;
it is not provider-ownership proof. Validate it again under the exclusive profile
lock. Before any restored worker starts, preserve the previous configuration and
record the complete grant/digest in the new recovery segment, then update only
the active route with its migration provenance. Never rewrite historical routes
to force a validation pass or pass this host-only authorization to the model.
Each migration preserves the exact authorization bytes beside its original
configuration snapshot. Existing CLI-upgrade and compatibility records accept a
changed route only through that complete verified lineage; their source records
and historical URLs are never edited to make a new route pass.

An explicitly authorized, evidence-backed Anthropic gateway repair may normalize
only `metadata.user_id` from the native JSON-string envelope to the same original
CLI session UUID. Default forwarding preserves this optional field unchanged.
The host-only compatibility record pins the run, session and preserved prior
configuration; recovery revalidates it under the exclusive profile lock. The
original model, route, profile, protocol, task, signed thinking, tool history,
allowances and all confirmed/unknown use remain unchanged. A different or invalid
session identifier fails before an upstream call. Record each transformation
with the original identifier's hash, never its device/account contents. This
compatibility treatment may change provider affinity or cache behavior; account
actual use normally. Unit tests are separate from full-request API acceptance
and from resumed native development acceptance.

An explicitly authorized run-bound Anthropic replay repair may restore an
original Bash command when the official CLI removes only a leading absolute
`cd ... &&` prefix during history replay. The same tool-use ID and name must
identify a complete, error-free response from the selected model. All other
input fields must match; broader changes fail before paid forwarding. Recovery
reconstructs originals only from native gateway response journals, excluding
diagnostic generations. Live complete responses supply later originals. The
repair defaults off, requires the original session compatibility record, and
preserves signed thinking, tool results, CLI history and all usage. It changes
only the forwarded historical input; it never executes a replacement tool call.

An explicit operator request to pause after HTTP 400 may enable the host-only
boolean `model_gateway_stop_on_http_400` for the original development. Recovery
validates it again under the profile lock. The first upstream HTTP 400 revokes
the model lease before the original response is relayed and its usage settled;
later native fallback requests cannot reach the paid upstream. Preserve the
original response and any unknown usage. This policy defaults off so other
clients retain their existing compatibility handling; it does not change 5xx
recovery, allowances, deadlines, gameplay or final-test behavior.

## Closed-run state

The internal development runtime can restore a hash-verified workspace archive
into a new clone of the same pinned baseline, preserving its original Linux
workspace path and official CLI history. Restore is forbidden for final-test
roles and beneath an existing supervisor. Validate profile/development identity,
archive bytes, file types, scoped member paths and links before native mutation;
recheck restored file bytes and new UID ownership before worker startup. Links
outside the workspace are limited to the exact configured baseline interpreter
or agent executables and must resolve to root-owned files without group/world
write permission. Scoped connection files may be refreshed without replacing CLI
history; ordinary staging still rejects existing directories. Resume adapters use
an explicit original session UUID and reject a different native session identity.
The controller acquires the profile lock, checks actual recorded owners and
registered clones, validates original configuration/task/candidates, reconstructs
durable episode and gateway ledgers, and restores the original finite deadline.
It snapshots the previous configuration and appends a distinct recovery segment;
ambiguous requests and game operations are never replayed. A live owner or still
registered source clone fails closed without stopping it. Automatic attachment
to a retained runtime and hard-crash reconciliation of a still-running status
are not implemented. Legacy finite runs without a recorded original start are
refused. Managed dependency payloads remain reconstructable,
and an interrupted game's process memory is not restored by workspace recovery.
Subsequent play uses the registered cold start. Keep the interrupted episode
and its resources in the original ledger.
[Recovery commands and grants](OPERATOR_WORKFLOWS.md#closed-run-recovery)

## Dependencies and runtime isolation

WSL dependency preparation is separate from final program execution. A Python
submission may declare `dependencies.environment` with exact named `requirements`,
public HTTPS `indexes`, and hashed `assets`. An asset has a safe relative `path`,
SHA-256, and either a public HTTPS `url` or a development `source_path` inside the
run workspace. The host resolves wheels in the target Linux interpreter, captures
asset bytes, and freezes the full transitive wheel/asset hashes, Python target,
and baseline identity as `environment_lock` in the program identity. Run-specific
asset source paths are evidence only, not part of the frozen identity. The
64 MiB/4096 code-file limits remain unchanged; large dependency bytes live once
in the host-owned content-addressed artifact cache.

Before starting gameplay, development evaluations and independent final tests
rebuild a run-local venv from the frozen lock. Final preparation starts in a fresh
clone, allows a bounded public setup phase without an agent, gateway credentials,
or submitted program, and uses only the fixed host installer. The controller
quiesces the installer's entire child cgroup, atomically restores and verifies the
canonical final network-denial policy, then runs dependency verification and the
submitted program. A preparation, hash, deadline, cleanup, or seal failure must
not start the game. Preparation time remains inside the existing wall deadline;
no gameplay tics or model calls are charged for installation. Package setup is
not LLM inference authorization. These checks retain the existing cooperative
threat-model limitations and do not make WSL results benchmark-eligible.

Until real development, independently invoked no-LLM final test, cleanup, and
isolation canaries pass, WSL results remain
`autonomous_benchmark_eligible=false`; a successful CLI exit or bridge call is
not isolation or experiment acceptance.

WSL supervisor startup acquires its configured Windows loopback control endpoint
with exclusive address use. Windows bind errors 10013 and 10048 may be retried on
that same address and fixed port for at most ten seconds; other bind errors fail
immediately. The retry does not use address reuse, select a fallback port, start
the worker before the bind succeeds, or reset game, model, or wall deadlines.

Every WSL development or final-test supervisor must run in a private mount
namespace created by a root-owned runtime script before dropping to the unique
run UID. The namespace replaces `/tmp`, `/mnt`, `/run`, and `/dev` with private
mounts, hides WSLg, Windows-drive, Unix desktop, and GPU entrypoints, and provides
only the essential device surface required by Bash, Python, OpenCV, stdio MCP,
and ordinary child processes. Before shadowing a resolver target under private
`/mnt`, `/run`, or `/tmp`, the root-owned script must open the existing readable
resolver configuration and then recreate the same canonical target only inside
the private mount. The private copy must be root-owned, read-only mode `0444`,
and independently verified through `/proc/<pid>/root`; unexpected resolver
targets fail closed, while regular targets outside the shadowed paths remain
unchanged. Acceptance must observe public DNS success in development and DNS
denial in final test, separately from direct-IP TCP checks. The host must verify
the worker UID, process group, base cgroup, distinct mount-namespace identity,
hidden desktop entrypoints, essential devices, and resolver through
`/proc/<pid>` before accepting its handshake. A
suspended child must match the worker mount namespace and expected UID, process
group, and base cgroup before the host assigns its nested cgroup; the host then
rechecks the nested cgroup and namespace before resume. Failed verification is
fatal. These checks reduce accidental or cooperative exposure but do not make
WSL a complete boundary against malicious same-UID races or kernel/WSL attacks.
They must never unmount, delete, or change permissions on shared WSLg paths, or
modify the operator's `.wslconfig`, original distribution, or hash-pinned
baseline.

The effective model/program viewport is persisted before runtime setup and
frozen into submitted program identity. Codex uses 1280x768 and Claude Code uses 768x461; independent
tests recover the viewport from the frozen program rather than guessing it.
Same-process CLI transport recovery remains distinct from explicit closed-run
archive restoration. Both retain the original workspace and accounting;
it never resets the game budget or extends the experiment deadline.

The host selects streamed output only for the configured, verified development
Codex or Claude Code CLI. Its worker drains at most 8 MiB of raw output per poll;
further bytes remain behind pipe backpressure for later polls. Cumulative output
bytes remain recorded, and complete redacted native events are retained without
changing model-visible images. Codex still rejects an individual JSONL event
above 4 MiB. Submitted programmes and dependency installers retain the 8 MiB
cumulative output fuse; final-test launches cannot select streamed CLI output.
These transport bounds do not change model-token, gameplay-tic or wall budgets.

Codex Code Mode uses a 3600000 ms default exec yield, matching the existing
3600-second MCP timeout. Long host startup/evaluation calls can then complete
without recurring model turns solely to poll a yielded cell. Keep the Code Mode
host available; disabling it in the pinned CLI makes its tools unavailable.
The effective wait settings are recorded in agent_runtime.tool_wait. This changes
the default tool-return behavior, not experiment deadlines or accounting, and
does not prevent an agent from explicitly requesting a shorter yield.

Owned cgroup cleanup reads `cgroup.events` populated state rather than the
reported size of virtual `cgroup.procs` files. It waits within the existing
five-second cleanup allowance, retains failed child handles for shutdown retry,
and removes the run network policy only after its cgroup has been removed.
Only validated run/child paths are accepted; already absent resources are
idempotent cleanup, while live or unexpected resources fail and preserve evidence.
Supervisor shutdown reports failed child cleanup and retains only those handles
for a later shutdown retry; closing the transport does not silently discard them.
A failed run-cgroup removal reports its exit code and a bounded classification
(populated timeout, numeric errno, or unclassified), without copying guest stderr.
These diagnostics and retry bookkeeping do not establish the cause of an earlier
native cleanup failure or change gameplay, scoring, or cleanup time allowances.

Generation requests use a finite 30-minute upstream HTTP timeout so large
multimodal turns can finish without changing their images, context, or prompt.
Token-count probes retain their shorter timeout. CLI retries and continuation
remain independently bounded and audited.

The official Claude Code development CLI receives a finite 31-minute API timeout
and 30-minute first-byte, byte-stream idle, stream idle and underlying Bun HTTP
socket idle windows. `BUN_CONFIG_HTTP_IDLE_TIMEOUT` uses seconds; the CLI timeout
variables use milliseconds. These settings
apply on initial and same-session recovery launches and are recorded under
`agent_runtime.model_request_timeouts`. They allow the host's existing upstream
timeout to settle before shorter native watchdogs retry a slow request. No retry
count, model context, experiment allowance or deadline changes. These model
transport variables are forbidden in final-test launches.

The model gateway accepts request bodies up to 64 MiB by default, including
inline image history from the official CLIs. The same bound applies after
normalization and host instruction insertion; explicit smaller caller limits
remain enforced. Oversized requests are rejected before upstream forwarding.
This transport bound does not increase model-token, gameplay or time allowances,
and restored developments retain their cumulative accounting and CLI session.

Usage-accounting failures use the same transient recovery path as other
retryable transport failures. Missing usage is recorded and returned as a
retryable upstream-usage-unknown error, but does not permanently revoke a usage
mode gateway based on its count. The gateway does not automatically resend the
same ambiguously accepted request. CLI-native retries and adapter recovery may
issue later requests, which can be billable duplicates. Confirmed usage limits,
deadlines, cancellation and explicit revocation still stop recovery; strict
accounting still fails closed on the first unknown result.

Automated Claude Code development passes `--autocompact auto` on initial and
same-session recovery launches. Compression calls use the ordinary metered
Anthropic route and remain subject to gateway budgets. Configuration is not
evidence that a compression threshold was reached or that a call occurred.

Codex development retains CLI-owned automatic compaction and explicit metadata
for configured model aliases. Compaction is metered through the same host gateway.
Strict accounting keeps compaction unavailable because its output has no finite
reservation bound. Same-session Codex recovery preserves the original workspace,
budgets and submission state; it is distinct from operator-invoked archive recovery.

Each baseline/profile declares its installed Linux agents. An agent missing from
that profile fails before a paid launch. Development passes only a run-scoped
gateway token, a private CODEX_HOME or CLAUDE_CONFIG_DIR, and scoped bridge leases.
Final test grants no model environment and makes managed agent executables
root-only before running a submitted program. Old pinned baselines are preserved;
Codex installation creates a new baseline and profile with package/file hashes.

From the first generation request, the host appends a persistent system-level
delivery and budget instruction. It defines the selected executable as the primary
outcome, distinguishes the independent model-token, gameplay-tic and wall-clock
limits, and requires enough budget to be reserved for writing, `submission.save`,
a bounded `submission.run` with zero program return code, and `submission.finish`.
Direct play and research are not deliverables, and remaining game tics do not
justify exhausting model tokens. This is a prompt-level requirement, not a claim
that natural-language instructions enforce submission.

Each automated generation request also receives a host-owned model-budget
snapshot taken before that request: configured limit, accounted and confirmed
usage, known remaining tokens, usage completeness, latest settled request size,
advisory request-count estimate, and remaining gateway deadline. Missing usage
keeps remaining tokens and the estimate unknown. This exposes the same planning
evidence as `bench.status` without requiring another tool turn. Native CLI or SDK
token counters, including `total_tokens` reminders, do not grant experimental
run tokens. Their original content remains preserved; the snapshot identifies
the host run budget explicitly. Snapshot instructions are metered normally and
do not alter admission, accounting, resource limits, or the 70% warning.

For a finite model-token limit, the host injects one finalization warning into
the next admitted generation request after confirmed usage first reaches 70%
of the configured limit, leaving approximately 30% for finalization. The
instruction is appended to the protocol's system
or instruction field so it does not disturb tool-result ordering, and its input
tokens are included in normal provider usage. It makes finalization mandatory:
the agent must stop optional research, preserve or create a runnable fallback,
perform the smallest bounded `submission.run` needed to establish a zero program
return code, repair execution errors, and call `submission.finish` on that evaluated
candidate. Weak gameplay performance is not a reason to withhold it.
The warning neither extends a budget nor authorizes idle tic consumption. It is
best effort: a single response may jump from below 70% to the limit, leaving no
admissible request in which to deliver it.

Development resource budgets are hard upper bounds, not required consumption
targets. `submission.finish` may explicitly select any saved candidate while
game tics, model tokens or wall time remain. It still rejects selection while a
submitted program is running, validates the candidate before side effects,
stops an active exploratory episode, freezes the selected archive and revokes
model access. Unused resources remain unspent and are reported as outcomes; the
host never auto-selects a candidate. A clean CLI exit without explicit selection
receives bounded same-session reminders even when zero game tics remain. Those
reminders ask for selection or useful work and must never require spending tics.

A human may additionally configure a method-neutral development review guideline
with `--minimum-development-fraction`. Its fixed notice asks for meaningful work
until either confirmed model-token usage or cumulative development tics reaches
that fraction of its original allowance, and permits voluntary selection after
either threshold. It forbids pointless consumption and retains all ceilings and
infrastructure stops. This is a model instruction with operator review, not a new
resource allowance or a runtime hard gate on `submission.finish`.
Adding it to a closed run requires a configuration-bound host budget-notice
authorization, rechecked and recorded under the profile lock. Preserve the
original task and append the fixed notice to the recovery message; never supply
independent final-test feedback or arbitrary strategy text through that grant.


Native Linux Claude Code retains its default system prompt and built-in tools,
including Bash. It receives the public development task and persistent host
gateway budget/delivery instruction. No Windows shell workaround is configured.
The supplied game bridge provides the stepped actions and observations.
The host owns startup, scoring, accounting, archives and upstream credentials.

Deployment must not use LLM inference. No credentials/gateway plus network denial
does not prove absence of an embedded local model. Artifact audit and real
file/process/network/desktop canaries remain acceptance items. A passing runtime
entrypoint canary does not replace inspection of the frozen artifact for embedded
model weights or inference code. Prompts are not security or budget enforcement.

## Public prompts and credentials

Code development is method-neutral: do not steer the agent toward image
processing or memory reading. Read-only game state, including hidden state and
game-process memory, is permitted by task rules, but access must not be claimed
available without verification. Progress must come from normal gameplay inputs;
direct state/save/score manipulation and injection/internal-function calls that
bypass those inputs are forbidden. Evaluator internals and credentials remain
host-only. Distinguish policy permission, audit and technical enforcement.

Task templates live in benchmark/code/prompts/development.md and are rendered by
benchmark/code/prompts.py. Reuse benchmark/spec.py's public gameplay and shared action protocol; never
interpolate complete scenario YAML or host configuration. Expose task title,
gameplay, objective, episode contract, public controls, tic semantics and the
explicit run budgets. Startup paths, saves, scorer/OCR parameters and credentials
are host-only. Save task_prompt.md in the run evidence. The selected CLI receives this as
its initial task, not as a replacement of its own base instructions.

Development also stages this client as mcp/c4g_client.py and sets the coding
agent's PYTHONPATH to that run-local directory, replacing any inherited host
PYTHONPATH. C4G_PYTHON names the pinned interpreter for shell debugging. Scripts
may import c4g_client from project subdirectories without copying it. Python
isolated/environment-ignoring modes do not use this PYTHONPATH. MCP still starts
through mcp/client.py in isolated mode. Final/submission execution injects the
client beside the Python entrypoint and does not depend on development paths.
Exploratory scripts share the active episode and budgets; no concurrent MCP
controller, automatic reset, or frozen-bundle acceptance is implied.

The standalone Python Client returns raw result dictionaries. Images are encoded in
`observation={mimeType,data}`; only the MCP adapter emits `content` image blocks.
The supplied `observation_bytes(result)` helper validates and decodes the raw
image payload and is also used by the adapter. An ended result may omit an image;
a missing active observation or malformed payload raises BridgeError. Image
codec validation remains the consumer's responsibility. Keep the rendered Python
example executable in tests through Client.call, not just checked as prose.

The selected coding-agent child receives an isolated HOME and CLI-specific config home, and only
scoped gateway/game credentials. Never copy operator auth.json, config.toml or
litellm.config.yaml into the guest. Host gateway injects the upstream key. A scoped
bearer is usable by code in the same guest; do not call it a secret from that agent.

Do not silently disable strict token accounting to accommodate an incompatible
provider. Quota and routing changes must stay within authorized scope and budgets;
report their implications. Paid tests follow root authorization rules.
Code-only artifact acceptance must not be inferred from blocked networking.

Submission paths identify a dedicated source directory inside the run workspace,
with entrypoint arguments relative to that directory. The workspace root contains
CLI configuration, helper links and bridge resources and is not a program bundle.
Do not include those resources, credentials, caches or virtual environments.
WSL tree export rejects symlinks and special files, identifies the offending
relative path, and never silently skips them to make a bundle acceptable.

Automated development accepts explicit --reasoning-effort. Claude uses --effort
on initial launch and same-session recovery. Omission preserves the CLI default;
configuration records the selection. Provider support still requires real
acceptance. The development launcher exposes -ReasoningEffort.
