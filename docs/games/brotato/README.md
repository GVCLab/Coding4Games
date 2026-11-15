# Brotato

The formal `brotato` scenario starts a fresh run. Launch through the local
Steam client for app `1942280`, with the account signed in. The game process
is `Brotato.exe`. Registered cleanup uses the active account's run state
under `%APPDATA%/Brotato`. No checkpoint ZIP is required.
[Driver](../../../games/brotato.yaml)

- Verify the 1280×720 HUD and the recorded menu path into Wave 1. The default
  development input is `gamepad-kbm`.
- The episode cap is 10000 tics at 10 Hz. Scoring uses validated wave
  progression and materials collected during combat. The registered run-lost
  screen ends the task.

[Scenario](../../../coding4game_gui/benchmark/scenarios/brotato.yaml)
