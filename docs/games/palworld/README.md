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
20 or later and download the
[pinned Palworld Save Toolkit source](https://github.com/zlmitchell/palworld-save-toolkit/tree/e71370ede118311e1a2b354caadb6d527ca9bcd2).
Extract it to the scenario's `toolkit_root`
(`out/third_party/palworld-save-toolkit` by default). The toolkit root should
contain `docs/js/migrate.js` and `docs/vendor/ooz-wasm/index.js`. Create
`package.json` at the toolkit root with the following contents so Node loads
the toolkit's browser JavaScript as ES modules.

```json
{"private": true, "type": "module"}
```

[Bundled adapter](../../../coding4game_gui/benchmark/windows/palworld_save_inspector.mjs)

[Formal scenario](../../../coding4game_gui/benchmark/scenarios/palworld_capture_penking.yaml) · [Driver](../../../games/palworld_capture.yaml)

[Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
