# Game requirements

The 15 formal scenarios have original-host development and independent
no-model test evidence. A new Windows host verifies its own game build,
account, task state, capture, input, timing and scoring. Choose the scenario in
**Run experiment**. Use the driver ID in `games.local.json` and when selecting a
driver under **Run auto-launch checks**.

| Game and setup guide | Scenario ID | Driver ID | Checkpoint ZIP |
|---|---|---|---|
| [Aim Lab](games/aimlab/README.md) | `aimlab` | `aimlab` | None |
| [Balatro](games/balatro/README.md) | `balatro` | `balatro` | None |
| [BattleBlock Theater](games/battleblock/README.md) | `battleblock` | `battleblock` | [`battleblock_current_account_golden.zip`](../snapshots/battleblock_current_account_golden.zip?raw=true) |
| [Black Myth: Wukong](games/black_myth/README.md) | `black_myth` | `black_myth` | [`black_myth_golden.zip`](../snapshots/black_myth_golden.zip?raw=true) |
| [Brotato](games/brotato/README.md) | `brotato` | `brotato` | None |
| [Forza Horizon 5](games/forza_horizon_5/README.md) | `forza_horizon_5` | `forza_horizon_5` | [`forza_horizon_5_golden.zip`](../snapshots/forza_horizon_5_golden.zip?raw=true) |
| [Kingdom Rush 5: Alliance](games/kingdom_rush_alliance/README.md) | `kingdom_rush_alliance_heart_of_the_forest` | `kingdom_rush_alliance_heart_of_the_forest` | [`kingdom_rush_alliance_heart_of_the_forest_golden.zip`](../snapshots/kingdom_rush_alliance_heart_of_the_forest_golden.zip?raw=true) |
| [The King of Fighters '98 UMFE](games/kof98/README.md) | `kof98_terminal` | `kof98` | None |
| [NBA 2K25](games/nba_2k25/README.md) | `nba_2k25` | `nba_2k25` | None |
| [Overcooked! 2](games/overcooked_2/README.md) | `overcooked_2_2_1` | `overcooked_2` | [`overcooked_2_account_compat_20261108.zip`](../snapshots/overcooked_2_account_compat_20261108.zip?raw=true) |
| [Palworld](games/palworld/README.md) | `palworld_capture_penking` | `palworld_capture` | [`palworld_capture_entrance_singleplayer_20260922.zip`](../snapshots/palworld_capture_entrance_singleplayer_20260922.zip?raw=true) |
| [Riders Republic](games/riders_republic/README.md) | `riders_republic` | `riders_republic` | [`riders_republic_current_account_20261110.zip`](../snapshots/riders_republic_current_account_20261110.zip?raw=true) |
| [Sekiro: Shadows Die Twice](games/sekiro_gyoubu/README.md) | `sekiro_gyoubu` | `sekiro_gyoubu` | [`sekiro_gyoubu_account_compat_20261107.zip`](../snapshots/sekiro_gyoubu_account_compat_20261107.zip?raw=true) |
| [Slay the Spire](games/slay_the_spire/README.md) | `slay_the_spire` | `slay_the_spire` | [`slay_the_spire_golden.zip`](../snapshots/slay_the_spire_golden.zip?raw=true) |
| [TrackMania Nations Forever](games/trackmania/README.md) | `trackmania` | `trackmania` | [`trackmania_golden.zip`](../snapshots/trackmania_golden.zip?raw=true) |

## Prepare local resources

Install the licensed game separately. Keep the checkpoint ZIP linked in the
table at its registered path under `snapshots/`. Use the [checkpoint guide](../snapshots/README.md)
to verify that the save matches your account and task before restoring it.
Set the local EXE and verified restore directory in `games.local.json`.
Always review the restore destination before running a checkpoint scenario.
For an account-bound checkpoint, explicitly set the restore directory for
your account and confirm that the archive is compatible. Several driver
defaults came from the original host.

For scenarios using virtual controller input or menu navigation, install
ViGEmBus. Drivers with `use_speedhack: true` require the xSpeed injector
and DLL matching the game's process architecture. All listed scenarios
except KOF98 use OCR during navigation or scoring. Run
`./scripts/start.ps1 -Setup -Ocr` for those scenarios, then verify their
OCR model and GPU runtime. The per-game page specifies additional native
or toolkit prerequisites when required. [New-machine setup](QUICKSTART.md)

Run `python -m coding4game_gui.doctor --game SCENARIO` for the static
resource check. Verify the registered frozen start, ordinary input,
timing and scoring on the intended build before development.
[Recording and experiment workflows](WORKFLOWS.md)

## Original-host evidence

Original-host logs, captures, and run archives are kept in local storage.
Use the [acceptance contract](contracts/EVIDENCE_AND_ACCEPTANCE.md) to
verify game startup, input, timing, and scoring on a new host.
