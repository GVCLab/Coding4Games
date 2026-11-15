# Overcooked! 2

The formal scenario `overcooked_2_2_1` uses driver `overcooked_2` and
`Overcooked2.exe`. Use the registered single-player **Story 2-1** start.
Supply the ZIP in the driver's `save_state.golden` and set the
verified account directory under `AppData/LocalLow/Team17/Overcooked2/`
in `games.local.json`. The scenario opts into the host's save-preservation
session for development and independent tests. [Driver](../../../games/overcooked_2.yaml)

- Prepare the 1920×1200 window with its full unobstructed client visible.
  The default development input is `gamepad-kbm`. Supply matching xSpeed
  components and verify actual freeze timing.
- Verify the 2-1 intro and fresh kitchen. The 1860-tic episode scores visible
  preparation milestones and up to 80 points from the net result score.
  Terminal confirmation uses three independent, consistent result captures
  spanning at least 0.5 seconds. Missing complete results settle to null.

[Scenario](../../../coding4game_gui/benchmark/scenarios/overcooked_2_2_1.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
