# Palworld

Use Steam app `1623730`, the registered single-player world and an active
compatible account. The process is `Palworld-Win64-Shipping.exe`. Set the
exact world folder under `%LOCALAPPDATA%/Pal/Saved/SaveGames/` for the
`palworld_capture` driver. Verify the account, world, dungeon, equipment and inventory
together before a restore.

Place the ZIP declared by the driver's `save_state.golden` under `snapshots/`.
Use a visible 2560×1440
DXcam client and `gamepad-kbm`. Confirm the frozen dungeon start and
capture of Penking and Pengullet. The host uses the bundled read-only Node.js
adapter to verify newly owned Pals from game-written saves. Install Node.js
and the separately acquired
`palworld-save-toolkit` at the scenario's `toolkit_root`
(`out/third_party/palworld-save-toolkit` by default).
[Bundled adapter](../../../coding4game_gui/benchmark/windows/palworld_save_inspector.mjs)

[Formal scenario](../../../coding4game_gui/benchmark/scenarios/palworld_capture_penking.yaml) · [Driver](../../../games/palworld_capture.yaml)

[Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
