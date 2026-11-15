# TrackMania Nations Forever

The formal `trackmania` scenario launches `TmForever.exe`. Use the
registered `snapshots/trackmania_golden.zip` for the A03-Race profile
under the local Documents `TrackMania` directory.
The checkpoint contains `Config/Default.SystemConfig.Gbx` and one
`Profiles/*.Profile.Gbx` file with the recorded profile.
[Driver](../../../games/trackmania.yaml)

- Use a native 1280×720 windowed client and `gamepad-kbm`. Supply the
  matching x86 xSpeed components and verify that the race clock stays
  frozen and does not catch up after resume. Startup verifies the captioned
  window and uses the recorded Alt+Enter toggle if presentation is fullscreen.
- Verify A03-Race and the registered handoff with about 3 seconds on the
  clock. The 1000-tic task scores the lap against the track's medal times.
  HUD finish detection requires the finish banner and a matching second time
  readout in addition to the stopped clock. The results panel is also accepted.
- The host retains passive native scoring frames every 3 tics during a held
  action, subject to its capture reserve. It replays those frames in order
  after play and preserves measured tics, including missed capture slots.

[Scenario](../../../coding4game_gui/benchmark/scenarios/trackmania.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
