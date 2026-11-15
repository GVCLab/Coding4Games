# BattleBlock Theater

The formal `battleblock` scenario uses `BattleBlockTheater.exe` from Steam.
Prepare the current Steam account's **Act 1, Scene 1 cage** start through normal
play. Restore `snapshots/battleblock_current_account_golden.zip` to the verified
account's Steam userdata directory for app `238460`. Set that directory in
`games.local.json` if needed. The calibration used Steam Cloud disabled for
this game. On a different account, finish the tutorial through normal play,
prepare the same cage start, and verify the matching checkpoint and menu route.
[Driver](../../../games/battleblock.yaml)

- Use a visible, unobstructed 1280×720 client with DXcam and `gamepad-kbm`.
- Supply the x86 xSpeed injector and DLL. This driver also uses the
  project-owned x86 IME helper. Build it with Zig 0.13.0 and the Windows
  .NET Framework C# compiler. Doctor verifies its generated manifest.
- Startup uses the hash-pinned helper to detach the owned game window's IME
  context after replay and before freeze. Startup fails on an identity,
  context, or helper cleanup failure. Shutdown attempts context restoration.

```powershell
python scripts/diagnostics/build_owned_ime_helper.py --zig C:/tools/zig/zig.exe --cache runtime/zig-cache
```

The open-loop menu sequence must hand off in the cage with the correct act
and scene numbers. Native numeral image checks account for the swinging
cards. Verify input, gem/HUD milestones and exit in the 3000-tic task.

[Scenario](../../../coding4game_gui/benchmark/scenarios/battleblock.yaml) · [Original-host evidence](../../SUPPORTED_GAMES.md#original-host-evidence)
