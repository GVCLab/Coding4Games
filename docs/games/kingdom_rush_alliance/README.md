# Kingdom Rush 5: Alliance

The formal driver and scenario are both
`kingdom_rush_alliance_heart_of_the_forest`. Launch
`Kingdom Rush Alliance.exe` and prepare Stage 3, **The Heart of the Forest**,
on Normal. Provide
`snapshots/kingdom_rush_alliance_heart_of_the_forest_golden.zip` for the task
profile under `%APPDATA%/kingdom_rush_alliance`.
Use `slot_2.lua` with Stages 1 and 2 completed on Normal and Stage 3 available
for its first clear.
[Driver](../../../games/kingdom_rush_alliance_heart_of_the_forest.yaml)

- Use the 2560×1440 windowed setup, Simplified Chinese UI, PrintWindow and
  `gamepad-kbm` development input.
- Check the frozen start for 20 lives, 500 gold, 0/10 waves and no towers.
- Verify formal victory and defeat with the registered OCR checks. Victory
  uses the remaining lives to score 90–100 points. The scorer reads lives
  directly from its calibrated native crop. A missing crop read stays missing.
  Independent final tests require the registered terminal evaluation.

[Scenario](../../../coding4game_gui/benchmark/scenarios/kingdom_rush_alliance_heart_of_the_forest.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
