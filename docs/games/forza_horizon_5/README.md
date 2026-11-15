# Forza Horizon 5

Launch app `1551360` from the local Steam installation and complete the game's
account sign-in. The game process is `ForzaHorizon5.exe`. The formal
`forza_horizon_5` task needs `snapshots/forza_horizon_5_golden.zip` for the
registered route. Use the matching account's Steam userdata directory for
app `1551360` as the restore target. [Driver](../../../games/forza_horizon_5.yaml)
The ZIP contains `remotecache.vdf` and the complete `remote/` directory.

- Keep the DXcam client visible and verify the 2560×1536 HUD calibration.
- The default input is `gamepad`. The frozen start shows an active route
  with a 1.5 km remaining-distance reading.
- The cap is 1000 tics at 10 Hz. Scoring measures progress toward that
  waypoint from the 1500 m baseline.

[Scenario](../../../coding4game_gui/benchmark/scenarios/forza_horizon_5.yaml)
