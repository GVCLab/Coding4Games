# Sekiro: Shadows Die Twice

Use the installed `sekiro.exe` and the formal `sekiro_gyoubu` scenario.
Supply the ZIP declared by the driver's `save_state.golden` for the correct
account under `%APPDATA%/Sekiro/`. Configure the exact account directory in
`games.local.json`. The scenario opts into the host's save-preservation
session for development and independent tests. [Driver](../../../games/sekiro_gyoubu.yaml)

- Use the 1920×1080 windowed client with `printwindow_visible` capture and
  raw `gamepad` input.
- Confirm **Reflection of Strength: Gyoubu Masataka Oniwa**, two boss lives,
  full player health, 10 gourds and 18 spirit emblems at handoff.
- The cap is 4100 tics. Confirm both lives and the native defeat or victory
  evidence before comparing the 0–100 progress scores.

[Scenario](../../../coding4game_gui/benchmark/scenarios/sekiro_gyoubu.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
