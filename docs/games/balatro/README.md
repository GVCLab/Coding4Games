# Balatro

Install the build that launches `Balatro.exe`. The formal scenario
`balatro` uses a fresh Ante 1 run. The host prepares the documented settings
template and cleans up any in-progress run as part of the registered start.
No checkpoint ZIP is required. [Driver](../../../games/balatro.yaml)

- Startup requests a 1600×900 window. OCR regions were calibrated at
  1536×864. Confirm the complete labeled HUD and correct startup geometry.
- The default development input is `gamepad-kbm`. Supply the matching xSpeed
  timing components and verify the first ordinary click after frozen handoff.
- The episode cap is 1000 tics at 10 Hz. The recorded menu tail plays before
  the frozen start check.

## Scoring

After play, `latest_valid_frame` scans the retained lossless frames backward
for the latest complete labeled Ante, Round, and current round chip readings
in `play`, `cash_out`, or `blind_select`. Skip shop/deck overlays and
incomplete HUDs. An Ante fraction must start inside the calibrated numerator
region so a lone denominator is rejected.

The score is `1000 × max(Ante − 1, 0) + 300 × max(Round − 1, 0) + current round chips`.
If every frame is unreadable, the score is null. OCR runtime or evidence
errors remain failures. The host stores the readouts, frame index, and
skipped count in `score-settlement/<episode_uid>/settlement.json` and writes
`selected.png` when a frame is selected.

## Local settings

Development and independent testing back up the original Balatro data and
restore it after the game closes. The run records the paths, hashes, and
restoration status in `game-settings/settings-session.json`. After an
interruption, confirm the old run has stopped and recover in the same Windows
environment with `python -m coding4game_gui.benchmark.windows.settings_session --recover PATH`,
using that journal as `PATH`.

[Scenario](../../../coding4game_gui/benchmark/scenarios/balatro.yaml)
