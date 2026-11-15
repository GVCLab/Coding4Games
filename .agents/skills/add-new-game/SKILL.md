---
name: add-new-game
description: Onboard or recalibrate a real Windows benchmark scenario, including its driver, task state, auto-launch, scoring, and no-model acceptance.
---

# Onboard a Windows benchmark

Read root [AGENTS.md](../../../AGENTS.md) and the [Windows game
protocol](../../../docs/contracts/WINDOWS_GAME_PROTOCOL.md). Inspect the
worktree and run the static preflight before launching anything.

```powershell
python scripts/calibration/add_new_game_preflight.py --game <driver> --scenario <scenario>
```

1. Resolve install, executable, process, capture, and save candidates through
   diagnostics. Ask the operator for the task, exact starting state, success
   and failure conditions, score semantics, and difficulty/loadout. Have them
   complete any required login or consent and prepare the selected in-game
   state. Verify the resolved save destination before an approved restore.
   Choose the verified deterministic cleanup for a reproducible no-save task.
2. Add the driver in `games/<driver>.yaml` and the task in
   `coding4game_gui/benchmark/scenarios/<scenario>.yaml`. The driver declares
   the process, capture, physical actions, launch, recorded `menu_skip`, and
   default scorer kind and implementation. The scenario declares the
   gameplay guide, objective, episode contract, `runtime`, `startup`,
   `lifecycle`, and task scoring. Set `runtime.tics_per_second` explicitly.
   Gameplay action durations use tics. Recorded startup waits use seconds.
   Keep scorer choice and calibration grounded in the approved task evidence.
3. Use [writing-game-auto-launch](../writing-game-auto-launch/SKILL.md) to
   record the route and verify the frozen handoff. Ask the operator to
   demonstrate the menu path when its in-game steps require their input.
4. Follow the [scorer workflow](references/scorer-workflow.md) to collect
   human-play evidence and replay it offline. Ask the operator to identify
   task-relevant state changes when the observations are ambiguous.
5. Re-run preflight and focused tests, then verify repeated serial cold starts
   and a no-model smoke using the intended input protocol. `CONFIG READY`
   confirms static consistency. Record the separate evidence for the actual
   start state, ordinary input, timing/freeze, scorer behavior, episode
   termination, and close/relaunch. Label any still-unverified gate.

   ```powershell
   python -m coding4game_gui.benchmark.windows.diagnostics.smoke --scenario <scenario> --episodes 3 --windows-input <protocol>
   ```

Keep local installation paths and approved saves outside the source release.
Maintain the per-game requirements page under `docs/games/`. Adding a formal
scenario to `coding4game_gui/catalog.py` requires confirmed release scope.
Run paid development only within the authorization in root `AGENTS.md`.

## Controller guide

Put the prompt-facing guide inside the scenario's `gameplay` block. Describe
the verified game-default Xbox/controller preset and confirm restored settings
retain it. Use this table shape.

```markdown
Controller guide (game-default controller preset; context-sensitive):

| Input token | Gameplay meaning | Menu / contextual meaning |
|-------------|------------------|---------------------------|
```

Cover `neutral`, `LS[x,y]`, `RS[x,y]`, `LT[v]`, `RT[v]`, and each driver
button. List applicable buttons in canonical order (`a`, `b`, `x`, `y`, `lb`,
`rb`, `ls`, `rs`, `dpad_up`, `dpad_down`, `dpad_left`, `dpad_right`, `start`,
`back`, `guide`). Explain relevant tap, hold, combination, and context
conditions from the installed build, saved settings, on-screen prompts, or
observed input. Mark unresolved mappings in the calibration evidence. Use raw
physical tokens and obtain approval for any task-specific remapping.

## Human-play recording

```powershell
python .agents/skills/add-new-game/scripts/record_scorer_session.py --scenario <scenario> --until-stopped --marker 'F6=<label>'
```

The recorder uses the registered startup and replay mode. If inspection is
needed, pass `--start-file OUT/start.request` in a fresh output directory.
Review `02_after_menu_skip.png` while the task is frozen, then create the
fresh start file. Verify status and growing frame/event counts before
handing the game to the operator. Have the operator manually play and retain
that same recording until they finish. Use markers defined by the task.

Create the printed `stop.request` when the operator hands control back.
Wait for clean termination, then inspect the milestone images, events, and
metadata. Successful stopping leaves the game open. Capture failure after
handoff releases capture and input resources and preserves the failure
receipt. Treat partial failed recordings as diagnostic evidence.
