# Evidence and acceptance

Verify each host's startup, ordinary input response, frozen desktop capture,
scoring, and clean WSL2 program execution separately. Required terminal evidence
failures close the session and retain the null score, consumed tics, and partial
diagnostics. For `startup.preserve_player_save` tasks, copy and hash-verify
the original save tree before restoration, retain generated saves, and restore
original bytes or absence after the run. An interrupted save journal and lock
require explicit recovery.

When finalized native terminal evidence and settlement output are on different
Windows volumes, settlement export copies the native files and manifests into
its `terminal_v2` child directory and records that relative reference. Copy
failures remain fatal. Game inputs, timing, terminal decisions and scoring are
unchanged by this evidence export path.

When the existing host-only `C4G_WINDOWS_EVENT_LOG` recorder is enabled, a failed
foreground acquisition retains the target and observed foreground HWND, window
class, PID, process basename/creation identity and modifier-key down states.
Missing windows, an unobserved activation and exceptions remain distinct; only
exception type names are retained. Observation failures never replace the
original focus result. The snapshot is taken after the failed request, not as
proof of the preceding failure's cause. It includes no window titles, paths,
keys other than modifiers, input, focus retry or deliberate wait. Successful
focus adds no diagnostic native queries. With the recorder unset, failed focus
also adds no observation queries. Software evidence for this recorder does not
establish native focus repair, capture acceptance or input/timing parity.

Hard-pause input-pulse failures retain the existing fail-closed guard. A failed
Windows suspension may append its PID, failed OpenProcess error or NTSTATUS,
and a best-effort process creation/status observation to that public error.
Identity is read only after the failure; it does not prove ownership at the
earlier native call. A simultaneous capture failure reports only its exception
type, never arbitrary exception text, paths or credentials. Successful pulses
add no process-identity query, input, suspend/resume call, retry or deliberate
wait. This diagnostic evidence is not a root-cause finding or native repair
acceptance. Preserve all prior invalid scores and consumed tics.

Pure-freeze diagnostics use independent desktop capture while the target stays
hard-suspended. Cached frames cannot prove containment, and PrintWindow can
block waiting for the suspended message loop. Record the client rectangle,
captured monitor intersection and any clipping; reject covered surfaces or
geometry changes. Failed diagnostic runs retain partial results, consumed tics
and a failure screenshot when available, then perform normal game cleanup.
Failed frozen start checks preserve the already observed frame under
`out/windows_startup_failures/` before cold-start retry. Saving this evidence
does not resume the target or add gameplay input; a save error must not replace
the original failed readiness result.

The optional host-only `C4G_CAPTURE_ERROR_EVIDENCE_DIR` absolute directory enables
one independent desktop PNG and geometry record per DXcam renderer after an
obstruction belonging to that same game process has already rejected a capture.
Detected foreign-process roots do not enable this diagnostic. The original
capture error remains fatal, and these pixels never become policy or scorer
observations. A vanished root after capture is recorded rather than treated as
proof that no obstruction existed. Diagnostic capture latency can add live time
to an already failed pulse; retain its measured usage and invalid score. Leave
this option unset for formal experiments and independent programme tests.

Migrated game registrations and software tests do not certify the current host's
startup, saves, input, scorer or isolation. Preserve the source commit and label
input/protocol differences before comparing against historical results.
Deferred scoring may retain the cumulative game tic alongside each lossless
frame and replay the nonnegative span through a specialized scorer's
`note_tics`. An optional scorer `finalize` hook executes once after all frames;
neither callback settles scores during gameplay or adds uncharged game time.

An opt-in `runtime.terminal_only_final_tests: true` requires registered terminal
detection. Development selection requires a zero-exit candidate evaluation that
reached that detector with a complete valid result. An independent final test
that exits/stops before it is `incomplete_task`, with `complete=false`, null
official score, retained partial score and all consumed resources. Terminal-only
behavior does not apply to scenarios that do not declare the flag.
The Windows detector reports registered defeat phases as `game_over`; this
counts as a terminal only when the environment is done with that matching
reason. A stop label by itself does not establish registered defeat evidence.

The development model gateway accounts upstream response usage, not a local
LiteLLM tokenizer estimate. `token_breakdown` contains confirmed cumulative
`input_tokens` (cache included), `cached_input_tokens`, `uncached_input_tokens`
and `output_tokens` (reasoning included where reported). Cache creation counts
as uncached input for Anthropic. Per-component missing counts live in
`token_breakdown_unknown_requests`; a subtotal with missing requests is incomplete,
not a zero-cost claim. Per-request audit fields use null for missing components.
Total-token budget enforcement is unchanged and includes cached tokens.
Token counts are not currency charges; provider pricing is a separate concern.
Opt-in restoration of a native Bash leading-cd replay records the tool-use ID,
source gateway request ID and original/restored input hashes. Complete raw
responses remain in their original host journal; transformation records do not
repeat commands, thinking or tool results. Diagnostic responses never become
native replay provenance or development-floor credit. A complete-request
diagnostic acceptance remains separate from resumed native development, game
acceptance and independent final tests.
Codex compaction calls pass through the same model gateway and their reported
usage remains charged to the run. Compaction can reduce later request inputs but
does not erase usage already recorded. The run configuration records that the
pinned Codex CLI/model catalog owns the automatic trigger and that no project
threshold overrides it; gateway audit records the actual `/responses/compact`
request and response when one occurs.
Strict prospective accounting records compaction as unavailable because the
compact endpoint has no output-token cap suitable for reservation.
Claude Code uses model-tuned `--autocompact auto`. Charge summary calls
reported on its ordinary gateway route and use the audit to confirm execution.

In usage accounting, a transport failure or completed response without valid
usage remains unknown and returns a retryable error without permanently revoking
model access. The gateway never resends an ambiguously accepted request itself;
the selected CLI and its adapter apply their ordinary transient-error recovery.
Repeated unknown requests remain auditable, keep remaining tokens unknown and
may have been billed. The confirmed subtotal still stops later requests when it
reaches the configured threshold, but after any unknown request that threshold
is not a guaranteed actual-spend bound. Strict accounting still stops on the
first unknown result.

Finite-token development exposes the 70% finalization-warning threshold
(approximately 30% remaining),
pending state and injected state in `model_budget`. Injection is recorded once
as `finalization_warning_injected` in the gateway audit with its request ID,
threshold and confirmed subtotal. This proves that the host added the warning
to the forwarded request; it does not prove that the provider completed that
request or that the agent followed the instruction. An undelivered warning when
one response crosses directly to the hard limit is expected and must not be
reported as successful finalization protection.

Successful early finalization is recorded as an explicit selected archive even
when `tics_remaining` is positive. Remaining tics and tokens are outcomes, not
evidence of incomplete work. Conversely, a clean CLI exit without
`submission.finish` is incomplete, and a token/deadline stop before selection is
a failure even if saved candidates exist. If the limit-crossing model response
already completed `submission.finish`, explicit selection takes precedence over
the concurrent token stop because the returned response and its tool effects are
preserved.

Root AGENTS.md authorization rules apply, including standing goal authorization
for skill-guided work. Paths beginning with benchmark/ or wsl2/ are relative to coding4game_gui/.
Other paths are repository-relative.

The Direct baseline keeps per-episode results, provider requests, and scoring
evidence. Its request and attempt logs use benchmark/direct/player_bridge.py,
C4G_LLM_LOG and call_context. Keep errors distinct from player action errors. The
shared LiteLLM retry mechanism remains bounded by its configured recovery window;
never multiply retries invisibly. Coding-agent CLIs use model_gateway.py through explicit protocol adapters and retain their own event
stream. Logs, screenshots and encoded response chunks can contain sensitive data;
base64/hex are not encryption. Do not embed raw secrets in reports or tests.

`submission.run` preserves the complete redacted child stream events in the host
`program_output` JSONL evidence. Its tool result also returns `program_output.stdout`
and `program_output.stderr`, each with a redacted UTF-8 `tail` capped at 4096 bytes,
the total raw captured `bytes`, and `truncated` indicating that the raw stream
exceeded the cap or that redaction expansion required clipping. The tails include events collected during bounded cooperative
exit and final drain, including nonzero program exits. An empty stream uses an
empty tail, zero bytes, and false truncation. Returned diagnostics must redact
the scoped bridge token, scoped gateway token, and host credentials already
available to the session, including across stream-event boundaries; they must not
disclose host evidence paths or evaluator internals. Host JSONL events redact
complete credential values present within each event. Byte counts are lengths of captured pre-redaction UTF-8 stream data,
not lengths of the returned redacted text. The host JSONL remains the authoritative
full diagnostic artifact, and output completeness still describes collection,
not gameplay success.

Current runtime/game diagnostics include scripts/acceptance/wsl2_runtime_canaries.py
and scripts/acceptance/wsl2_game_probe.py. They are
operator-only; never run them just because a test file imports their pure helpers.
Unit tests may use fakes and local test listeners without paid model calls.
Development replay reads the saved lossless observations at their recorded tics.
OCR-based scorers reuse host perception within one deferred-scoring episode.
An episode-local disk cache binds results to the full frame pixels, shape and dtype
and wraps only that scorer's fixed OCR engine. No cross-episode or cross-engine
reuse occurs. Detection and direct text recognition retain separate cache keys,
including recognition preprocessing options. The wrapper preserves both engine
operations. It stores text, boxes and confidence, never rewards; final scoring
still decodes every preserved lossless frame and applies the original ordered
state machine. Missing online reads fall back to ordinary OCR. Settlement records
cache hit/miss counts and releases the cache on completion or error; explicit
episode cleanup also releases it. Specialized game scorers keep their existing
perception paths. Cache data is disposable, not authoritative scoring evidence.
Report full-suite failures separately from current-path acceptance; do not delete
still-applicable tests to obtain a green report. Retired protocol tests may be
removed only with corresponding retired code and preserved backup/evidence.

WSL managed dependency payloads live under the reserved run-local
`.c4g-dependencies` directory. Workspace TAR export excludes that exact directory
after rejecting a symlink at the managed root; old evidence is never rewritten.
Dependency requests, frozen locks, installer stdout/stderr, return codes, timings,
post-install freeze/check results, and network transitions remain in host
`dependencies/` evidence, including failed preparation attempts. Wheel and asset
blobs are stored once by SHA-256 under `dependency_artifacts`, outside program
bundles and per-run exports. A cache reference alone is not a valid payload:
verify the recorded byte length and digest before installation and use pip's
hash-required wheel-only install. The saved manifest remains immutable;
`dependencies_verified` is a runtime result, not a retroactive manifest edit.

Startup calibration must verify the first ordinary input after policy handoff,
not only the presence of a HUD, a stable frame, a responsive window or a delivered
OS event. Record the expected game response and before/after evidence in separate
acceptance episodes; charge those actions normally. Do not inject hidden probe
actions into formal startup. A calibrated readiness dwell is scenario-specific;
its success in one game does not validate another game or a changed game build.
Report visual start checks, input delivery and observed game response separately.

When rescoring offline, record the source artifact and scorer version. Use the
registered settlement rule for the task and leave prior evidence intact.
[Game requirements](../SUPPORTED_GAMES.md)
