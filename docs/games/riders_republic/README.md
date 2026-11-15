# Riders Republic

The formal `riders_republic` scenario uses the Steam build (app `2290180`)
and the installed `RidersRepublic.exe` opened through the Windows desktop
shell. Sign in to the matching Ubisoft Connect account. Launch the game once
to create `Documents/My Games/Riders Republic/Default/options.ini` before
the host can apply the calibrated presentation. [Driver](../../../games/riders_republic.yaml)

- Supply the checkpoint identified by the driver's `save_state.golden` for
  that account and set `riders_republic.save_state.restore_to` in
  `games.local.json` to its actual Ubisoft `savegames/<account>/5780`
  directory. Set `auto_launch.launch_path` in the same entry when selecting
  a specific installed library copy. Development and independent tests
  opt into the host's save-preservation session.
- Use a visible 1600×895 window with DXcam and `gamepad`. The host requests
  English. Startup waits for the restored main window before applying geometry.
  The score label is calibrated in English and Chinese.
- The closed-loop menu sequence enters **Zen Mode**, dismisses the welcome
  overlay and verifies a frozen zero score. Each episode keeps the game's
  natural starting location and can use any sport.
- The 1000-tic task settles the highest trustworthy awarded trick total after
  play. It uses a fixed endpoint with no intermediate terminal checks.
  Crashes are ordinary play.

Verify the account, restore, startup, input and timing on each new host.
[Scenario](../../../coding4game_gui/benchmark/scenarios/riders_republic.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
