# Shared Windows game and action contract

## Captured-pointer controls

KBM also accepts `RMOVE[dx,dy]` (one relative mouse event, signed integer
device units in -1000..1000, not both zero) and `CLICK_CURRENT` (one left
click at the captured pointer). Relative deltas are not screenshot coordinates
and are not resized with the model viewport. Each is the sole control in its
ACT, runs inside the ordinary measured pulse and releases buttons afterward.
Gameplay `CLICK_CURRENT` uses the existing game-window ownership check;
trusted recorded startup keeps its existing behavior. Recorded `type_text`
search steps are startup-only and support nonempty ASCII queries.
The shared Code session accepts these two captured-pointer tokens through
`game.step(controls=[...], tics=...)` in keyboard/mouse mode, one token per
call. Other keyboard/mouse actions keep the structured `actions` form. The
ordinary backend parser validates relative deltas and rejects combined pointer
controls before input or gameplay accounting; both forms share the same pulses.
Windows startup typing delivers those queries as paired Unicode keyboard
packets so the active keyboard layout or IME cannot convert task names.
Partial OS delivery fails the step without retrying characters.

## Game-specific startup and scoring

The [game requirements](../SUPPORTED_GAMES.md) link each scenario's startup,
save, input, and scoring conditions. The registered configuration supplies
calibration and resource limits.

The Palworld capture scenario selects `kind: save` with
`implementation: save_verification`. Only the host reads durable game-written
save evidence; the verifier cannot change saves or inject internal game calls.
Toolkit/checkpoint dependencies must exist before running. A capture input
alone is not proof of capture. Visual and durable-state scoring retain their
registered task-specific meanings and resource limits.

## Startup and capture

`startup.no_save_cleanup: process_kill` limits cold-start preparation to
process cleanup. It requires a task with neither driver save restore nor a
scenario checkpoint. Operational acceptance still needs a live cold start.

When a configured Steam EXE path no longer exists, the host can relocate its
exact `steamapps/common/...` suffix using Steam's registered library list.
It never selects a different executable or guesses between multiple installed
copies. A missing Steam client path can likewise resolve to the registered
local `steam.exe`. This is path discovery, not startup or build acceptance.
An optional `auto_launch.launch_method: desktop_shell` asks the existing Windows
desktop shell to open that validated local EXE, with separately quoted arguments
and the executable directory as its working directory. It uses the normal `open`
verb without elevation or a launcher URI. The default `process` method retains
direct subprocess creation. Desktop-shell launches provide no bootstrap handle;
the shared prelaunch path still creates pads first and waits for the configured
game process/window. Shell failure is an error, not a reason to change methods.

`%STEAM_USERDATA%` in a restore target resolves to the active Steam account's
userdata directory. Operational loading fails without an active account;
metadata-only inspection may retain the unresolved marker. Never choose an
arbitrary account directory to make a restore succeed.
An explicit null `save_state.restore_to` declares an account directory that
must be supplied through `games.local.json`. Metadata inspection may proceed
without it. Runtime loading and save restoration require the verified target.
When the registry's ActiveUser is unavailable, account resolution may instead
use the completed logon identity in the current Steam client's connection log.
Require one installed root, one live process matching that root's exact steam.exe,
and a completed logged-on account event after that process's creation. Newer
disconnect or login-in-progress events, stale logs, partial writes and ambiguous
clients fail closed. Cached login lists and arbitrary save directories are not
identity evidence. This read-only fallback neither restarts Steam nor changes
registry/account settings; Brotato's existing account marker and run-only cleanup
remain enforced.

Shared cold starts wait for a persistent game window within the configured
window budget. A Steam session preflight never shuts down or restarts an
existing client: it waits for that client to reconnect within its existing
timeout, then fails while leaving it running if the session is unavailable.
Palworld's observed native NVIDIA dialog asks whether to open the driver
download webpage. Startup selects its verified No button and waits for the
actual game window within the same window budget. It does not install a driver,
suppress driver checks, or dismiss unrelated warnings or security dialogs.
Its optional EOS multiplayer error can arrive while lazy navigation OCR loads.
Recheck the existing narrowly detected modal immediately before each recorded
mouse click, in addition to the per-step guard. This is startup handling only;
it neither restarts Steam nor changes single-player progress or gameplay tics.
Shared cold starts follow a Steamworks bootstrap exit without treating the
handoff as a separate failed launch. Full verification and benchmark resets
use the same check. If the window disappears or is replaced during settle,
require the complete configured settle on its replacement. The remaining
window-wait budget plus the settle interval bounds this stage; replacement
does not reset the deadline. Apply window geometry after the complete settle,
to the final HWND before constructing the renderer or replaying input. A splash
screen may be fullscreen until account presentation is loaded. Presentation
preparation must not change the Steam client's DPI compatibility settings.
Desktop capture drivers must
reacquire the game foreground before the menu handoff hard-freezes it.
The declared window size refers to the captured client viewport. PrintWindow
captures an unscaled surface for DPI-unaware games, so startup uses the same
observed DPI conversion as that renderer to size the physical window. Record
both dimensions; do not resize a cropped or stretched image into acceptance.
Native client corrections are asynchronous, limited to three requests inside
the existing geometry deadline, and must reach the exact expected dimensions.
Cold starts discard the previous game's mouse HWND cache. Closing a DXcam
renderer releases its camera so the factory cannot reuse a stopped instance
for the next game process.
DXcam receives the selected HWND and PID, checks client geometry, foreground,
non-minimized state and a nine-point window ownership sample before and after
each snapshot. This is a best-effort occlusion check, not a proof against all
overlays. It reports the obscuring HWND, class and PID and never hides other
applications or forces focus while capturing. Missing frames are errors rather
than black or previous-image substitutes. It reverses measured Windows DPI
stretch and exposes native `frame_size`; startup applies the corresponding
physical client size. DPI-aware windows retain their original dimensions.
If pre-suspension capture fails, the shared driver discards its old observation,
hard-freezes the game and surfaces that error. It does not retry a renderer
against a suspended message loop or silently reuse the preceding ACT frame.
The opt-in `printwindow_visible` backend recovers a missing PrintWindow client
surface from the current visible desktop client. A one-pixel non-client edge
does not count as game content. The fallback requires the game foreground and
checks its client corners and center for occlusion before and after capture;
changed geometry or failed visibility checks raise an error. It reverses the
measured Windows DPI stretch to the native client dimensions and uses logical
client offsets for the unscaled PrintWindow surface. It does not advance the
game or substitute an earlier frame. The legacy `printwindow` backend and its
existing calibration remain unchanged.
`visible_client` uses the same guarded client capture and DPI conversion on
every frame without calling PrintWindow.
Navigation and scorer construction use the renderer's emitted native
`frame_size`, never the physical desktop dimensions. Active renderers cache the
last observation before hard suspension; a failed capture cannot substitute an
earlier ACT frame. Actual running-time accounting and tic limits are unchanged.

Image startup assertions may explicitly use `min_correlation` instead of
`max_mean_abs_error`, with bounded native-pixel `search_margin` and reference
`rotations`. These do not resize observations or advance the game. BattleBlock
uses measured numeral patches so swinging ACT/SCENE cards remain recognizable;
both task numbers must match. Constant references, invalid angles, unbounded
searches and ambiguous metric settings fail before launch. Omission preserves
the exact-region mean-error comparison.
An optional integer `[x,y]` `scorer.window_setup.position` places the outer
window in physical desktop coordinates during startup. Observe both client
size and position within the existing asynchronous geometry deadline and
retry limit. Omission preserves the prior position. BattleBlock requests
`[0,0]` to avoid the system surface observed at its former lower-right corner;
this does not disable notifications or change the client's native dimensions.
Foreground requests keep the host and game input queues separate. Do not use
`AttachThreadInput` or synchronous raise/restore calls against a loading game:
they can stall the startup timeout. Post restoration and asynchronous Z-order
requests, then verify the actual foreground HWND through the existing bounded
polling. A posted request is not successful focus evidence. A foreground HWND
that remains iconic still requires restoration and is not successful focus.

Menu replay must acquire the real game foreground before any navigation
perception or input; exhausted bounded focus requests fail and freeze rather
than reading the operator desktop. Palworld capture's recorded mouse path
enables the existing ALT unlock gesture for a background Steam launch and
rechecks focus immediately before its modal handling and mouse inputs.
Kingdom Rush presentation preparation writes only its
observed window dimensions, windowed/DPI preferences and `zh-Hans` locale;
save slots, progression and control bindings remain outside that operation.
Host save restoration and Kingdom Rush presentation preparation detect AppData
write redirection through a temporary file's resolved handle path. When a
desktop-app container redirects those writes, a hidden fixed worker launched by
the existing desktop shell verifies native writes before preparing the same
registered target. Retain its host-only request/response receipt. It does not
change the application's virtualization settings, restart Steam, or edit save
contents. A missing desktop broker or still-redirected worker fails preparation.

`startup.start_check.join_text_boxes` is boolean and defaults to false.
For a rectangular text check, true explicitly permits combining the OCR
boxes whose centers lie inside that rectangle. False retains single-box
matching. Score-phase rules keep their existing matching behavior.
`startup.start_check.image_templates` optionally asserts every named native
image region at the frozen handoff. Each entry supplies a repository-relative
`path`, an integer `[x,y,w,h]` `rect`, and a finite `max_mean_abs_error` in
0..255. References must have the declared calibrated client dimensions; live
frames with different dimensions are rejected without resizing. Compare BGR
pixel mean absolute error inside each region. Load and validate references
before launch. This is a startup assertion, not a scorer or terminal detector.
Declared closed-loop menu text destinations must be observed before continuing.
An OCR exception is not arrival evidence; exhaustion of the existing bounded
navigation retries fails startup rather than continuing on the wrong page.
An optional menu entry `retry_duration_seconds` selects a calibrated shorter
hold for subsequent closed-loop pad/key retries; the first hold retains its
recorded `duration_seconds`. It requires an explicit text destination and
active pad/key input, must be finite and positive, and cannot exceed that
recorded hold. Omission preserves the previous retry behavior. It is startup
metadata on `PadActionSpec`, confined to startup. Open-loop replay uses the
recorded timeline and verifies the frozen handoff.

An explicit startup.menu_skip_ocr mapping can provide navigation-only OCR for
a non-OCR scorer. Its reads do not change the save/image scoring mechanism.
Pad steps with expect/expect_absent also require that text destination rather
than accepting unrelated animation as input delivery. Open-loop replay retains
its recorded timeline and does not perform those checks.

`startup.menu_skip_ocr_rect` overrides only the navigation reader's crop;
settlement retains its registered OCR crop and rules. A neutral menu step
with `expect`/`expect_absent` keeps its recorded initial wait, then polls
without input within the existing six-attempt duration bound. Missing text
or OCR failure rejects navigation. Full launch diagnostics resume the frozen
game before cleanup, matching the reset controller's shutdown ordering.
An optional navigation-only `min_scene_luma` floor on a guarded neutral wait
also requires the measured whole-frame grayscale mean to reach that floor.
Palworld's entrance HUD appears over a black loading frame, so text alone
cannot authorize the next interaction. This guard uses the existing wait
deadline and sends no input; it is not a gameplay terminal or stillness rule.


## Experiment viewports

Claude Code development presents observations and replay images at 768x461.
Archived programs that declare a 1000x1000 viewport still resize along
both axes without letterboxing, with x and y in 0..999. This makes screenshot
pixels and program coordinates agree instead of applying an invisible normalized
coordinate convention only to model actions. A coordinate of 1000 is out of bounds.
Both MCP and Python Client.call use the selected viewport. Structured pointer
actions use that viewport and map once on the host to
canonical 1280x768 pixels, with endpoint-preserving per-axis scaling. Tool
schemas and the selected agent startup prompt describe the active viewport.
The archived square viewport's screenshot notices and pointer schemas describe
the model-native image-grounding convention (`[y,x]` points and
`[ymin,xmin,ymax,xmax]` boxes) into the action contract's named fields: `x` is
horizontal left-to-right and `y` is vertical top-to-bottom. The host must not
guess or automatically swap coordinates because correctly ordered calls are also
valid and indistinguishable by range in the square viewport.
All pointer types, including drag endpoints and scroll positions, follow it;
wheel increments and action tics do not change. Out-of-viewport coordinates
are rejected before game dispatch, not clamped. The host validates canonical
actions as before. The viewport is recorded in run configuration and immutable
program identity; candidate execution and separately invoked final tests retain
the same viewport. Legacy runs/programs without this field use 1280x768.
Other agent adapters, internal host evidence and scoring retain the canonical
contract. Arbitrary Read images/crops and direct desktop APIs are not transformed.
This is an explicit experiment viewport, not a guessed correction for every
model response.

## Episode budgets and terminal detection

Registered `runtime.max_tics` caps each development rollout, final test,
and matching baseline episode. The cumulative development budget is separate.
An explicit smaller cap applies. Larger requests leave the scenario cap in force.

Automatic detection follows `lifecycle.terminal_detection`. The fixed-duration
Aim Lab, NBA 2K25, and Riders Republic tasks settle after play with intermediate
terminal checks disabled. Their task guides give the endpoint and scoring rules.
Other formal scenarios use their registered ending detector.

Stepped runs normally inspect the frozen end-of-action frame. Sekiro Gyoubu
(`during_action_v1`) and Overcooked Story 2-1 (`during_action_v2`) also
inspect native frames inside the same held action and may end that hold on a
registered ending. Keep the confirming frames and measured live duration.

The shared Windows environment calls the registered scorer's `is_terminal`,
then records its phase, marks done, cancels remaining actions, and uses normal
host settlement and cleanup. No delta/settle call occurs during detection.
Terminal rules are game-specific: Brotato RUN LOST; Balatro/Slay the Spire
registered run-ending screens; Black Myth victory/defeat; Forza reaches a
visible remaining distance of at most 5 metres or its calibrated near-target
HUD-disappearance fallback. Ordinary intermediate rounds, shops,
level-ups and stillness are not generic endings. `kof98_terminal` uses the
registered first-fight outcome.
An explicit false value disables detection for diagnostics; normal runs keep it
enabled. Record this setting and preserve historical evidence and accounting.

Runtime scoring is deferred until each episode ends. During gameplay score is
null (pending), not zero. Preserve lossless scoring observations and settle once
in order. Startup start_check and registered terminal detection remain active;
stillness, unchanged-text and non-progress watchdogs remain disabled. Episodes
also end by explicit stop, program exit, configured resource limits, operator
interruption or infrastructure failure.

When a stepped episode has already become inactive because of its own detected
terminal state while its program child is still running, the host gives that
known child the existing shared cleanup budget, up to two seconds and clamped
to the original run deadline, to exit cooperatively. This wait sends no game
input, charges no game tics, makes no model request and never extends the run
deadline. A child whose exit is observed during the window is not killed.
Already closed children, expired deadlines and output-overflow failures retain
their immediate behavior. If the child remains alive when the bounded shared
cleanup budget ends, ordinary process-tree cleanup applies. Record the outcome
as `program_exit_policy` and the measured wait as
`cooperative_exit_wait_seconds`; these fields describe cleanup after the
terminal episode and do not change its score or terminal semantics.

## Actions and timing

`runtime.max_action_tics` is an optional integer from 1 through 1000. A lower
limit rejects oversized actions or batches before input, budget truncation,
or tic accounting. The same limit applies to development, independent final
tests, and stepped Direct.

Scenario files live in benchmark/scenarios/<scenario>.yaml with exactly: title, engine,
driver_game, gameplay, objective, episode_contract, runtime, startup, lifecycle,
scorer. Drivers live in games/<game>.yaml. Game knowledge and infrastructure stay
separate. Scorer kind and implementation must resolve explicitly; no implicit OCR.

The driver's actions mapping is the only fixed action declaration. Do not add
per-game action_profile/action_mode/action_abstraction selectors. Digital raw
controls use physical labels plus neutral; analog LS/RS/LT/RT are generic tokens,
not YAML-enumerated macros. No action duration_seconds/duration_tics overrides.
ACT tics is the gameplay hold-time source. Recorded menu_skip durations remain
real seconds. runtime.tics_per_second is a positive integer (compatibility default
10); changing it never silently rescales budgets or literal tic limits.

--windows-input gamepad and gamepad-kbm are mutually exclusive supplied protocols.
KBM only exposes NEUTRAL, safe physical KEY, MOVE, CLICK, RCLICK, MCLICK, DOUBLE_CLICK,
DRAG and SCROLL. Gamepad tokens are rejected in KBM mode. Policy coordinates are
1280x768 canonical integers, never clamped; shared infrastructure converts to
calibrated client / actual window geometry. A pointer action must be the sole
control. Standalone ESC (alias ESCAPE) is allowed for game-native menus;
it must be the sole control in an ACT. Keyboard excludes Delete, Alt and Win.
Mode-specific gameplay guides use C4G_WINDOWS_INPUT markers; KBM fails closed
without a verified guide. Do not invent timing or game semantic mappings.

Scenarios may opt into the public pointer-target ownership guard for gameplay
CLICK, RCLICK, MCLICK, DOUBLE_CLICK, DRAG and SCROLL. Immediately before each
button or wheel event, the host checks that WindowFromPoint resolves to the
game window or its child and that the foreground window is the game or its
child. If either check resolves to a foreign window, reject the action before
sending any pointer event. The public error may identify only the relevant
HWND, PID and window class; do not expose other window titles. MOVE remains
unguarded because it sends no button or wheel input. Raw calibrated startup
and menu navigation retain their recorded behavior and do not use this guard.

The guarded runtime requires an unlocked, unobstructed desktop reserved for
the game. This immediate ownership check reduces accidental input to overlays,
but it is a best-effort race check rather than an absolute isolation boundary.
It does not hide or close other applications, change focus, alter topmost
state, or add input delays. Operators must remove or disable persistent
overlays through normal reversible controls before formal runs.

An ACT is one complete live pulse: resume, publish state, hold, release, freeze.
Each requested ACT or structured action must contain 1..1000 integer tics.
Reject oversized actions before input and before budget truncation; reject the
whole batch if any action exceeds the cap. Do not silently clamp or split them.
Trusted foreground preparation for a stepped ACT runs after the game and time
controller resume, inside the measured live pulse, and before any policy key,
button or pointer action is published. Recheck cancellation after preparation
so a stop raised while native focus work blocks cannot release a late policy
input. A preparation failure publishes no policy input, still performs normal
release and freeze cleanup, and retains the measured live time already consumed.
Repeated identical actions are separate presses. NEUTRAL advances time; it is
not a required repair for missed release. Pointer requested duration includes
input delivery and remaining settle, with no hidden added gameplay wait. Retain
measured running seconds and round cumulative episode tics to the nearest integer
with halves up; debit differences of that counter, not per-action rounding.
A stepped episode's last executed action may exceed its resolved tic limit by
at most two tics without invalidating the score. Keep actual usage and overshoot,
stop at the original limit and never grant another action from this tolerance.
Development, final tests, and Direct share this rule.
In-action sampling skips a pending frame when the remaining hold time is no
greater than the sampling interval or twice the slowest processing time in that
pulse. The final frozen frame is captured for observation, terminal detection
and scoring. Actual running time determines charged tics and any overshoot.
CLICK, RCLICK, MCLICK and DOUBLE_CLICK release the button and leave the cursor
at the target. MOVE positions it for hover, DRAG releases the held button,
and SCROLL leaves the pointer at its target. Calibrated menu replay keeps its
registered pointer behavior.

Timing diagnostics compare requested time against the actual live window, not
against complete API wall time or a zero remainder. Keep launch/navigation timing
separate. use_speedhack requires real driver-specific calibration of freeze,
input pumping and resume. No uncharged wake delay without that explicit opt-in.

Direct Player replies are validated transactionally: syntax errors, no ACT,
invalid controls or max_burst overflow reject the whole reply while frozen and
charge zero tics. No prefix execution or truncation. Send ACTION_ERROR in the
same conversation, charge corrective LLM calls, and stop after three consecutive
invalid replies. max_burst counts ACT lines, not tics. Per-episode caps may be
None; global budget and final action truncation still bind where configured.

## Frozen startup handoff

Every Windows episode is serial and unseeded. Cold start kills residual game
process, restores a configured golden state or runs registered deterministic
cleanup, directly launches the executable, replays recorded menu_skip, verifies
start_check, plays, then closes. A no-save scenario omits save_state and golden
checkpoint. Startup replay mode must agree across calibration and execution.
Replay the complete recorded menu_skip by default, including its final neutral
wait, then hard-pause and verify start_check. The presence of start_check must not
implicitly remove recorded steps. Only an explicit, calibrated
`startup.menu_skip_preserve_tail: false` may replace the final neutral wait with
the frozen start check.

Do not add an extra readiness dwell without calibration evidence. If the frozen
start check still fails, advance only one-tic pulses between observations. The first
validated frame is the frozen tic-0 observation, unless a calibrated
`startup.start_check.settle_seconds` readiness dwell is configured (default zero).
That dwell advances only live one-tic pulses while the start conditions continue
to pass; a failed observation resets the dwell. Frozen OCR time does not count.
The first validated frame after completing the dwell becomes tic-0 for all arms.
Log the configured dwell and live startup pulses explicitly as startup time before
policy accounting. Keep readiness dwell scoped to calibrated startup.

After the frozen initial observation is captured, an optional scorer
`begin_episode(frame)` hook can initialize evidence-dependent state from that
same frame. KOF98 uses it to remember the initially healthy round before the
first ACT. It does not make a terminal decision, publish a score, advance time,
or add an intermediate stepped terminal check.
Recalibrate when startup changes. Do not add policy reset or restart actions.

Calibration trust comes from evidence, not a provisional/verified marker.
Scenario registration must not guess executable paths or manufacture calibration.
New game work follows the add-new-game skill and root authorization rules.
Goal authorization does not substitute for missing calibration evidence.

## Observations and episode evidence

Windows run names end in `_input-gamepad` or `_input-gamepad-kbm`, matching
`config.json`. Stepped Direct encodes model observations at 1280x768 with JPEG
quality 85 and caps request-side image history at 16 images and 8 MiB. Preserve
text history and native scoring/recording pixels. The coding CLIs own their
internal history policies.

Episode journals span pre-reset through result/ledger states; episode_uid-based
charging is idempotent. Recover interrupted prefixes as infrastructure_error,
exclude invalid scores, retain consumed tics. Readers may accept historical uid.
Do not silently drop failed/interrupted work or call a clean exit a submission.
Program finish selects one archived identity; later test is a separate cost and
score, never an automatic replacement of the selected artifact.

For scorer-specific settlement and artifact requirements, see the
[game guides](../SUPPORTED_GAMES.md) and [evidence contract](EVIDENCE_AND_ACCEPTANCE.md).
