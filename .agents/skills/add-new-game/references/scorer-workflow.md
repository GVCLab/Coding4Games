# Scorer Workflow

Use this workflow to create or repair a Windows benchmark scorer. Define the
process here; determine the actual signal and implementation separately for
each game and benchmark task.

## Rules

- Confirm what the score means before selecting an observation technique.
- Collect scorer evidence from a human playing the real task. Automation may
  restore, launch, navigate, and record, but an AI player must not generate the
  gameplay used to design or calibrate the scorer.
- Treat `scorer.kind` as the observation path: `ocr` reads text; `image` reads
  non-OCR pixels. Treat `scorer.implementation` as the concrete runtime scorer.
- Do not infer the observation path from genre or reuse another game's method
  without evidence from this game's recording.

## Procedure

1. Confirm the scoring contract with the user: score range and direction,
   incremental versus cumulative behavior, start value, success/failure,
   terminal conditions, and what must happen when the signal is absent.
2. Follow the skill's [human-play recording](../SKILL.md#human-play-recording)
   procedure. Have the operator play a representative task sequence while
   frames and relevant state transitions are recorded.
3. Inspect the milestone images and recording for the correct game state.
   Exclude desktop, launcher, overlay, wrong-save, menu-path, or other polluted
   frames before using the recording as scorer evidence.
4. Compare the human-play states and identify the smallest stable visible
   signal that expresses the confirmed contract. Select `kind: ocr` only when
   reading characters is essential; otherwise select `kind: image`. The image
   path may use any justified non-OCR visual analysis; this workflow does not
   prescribe a particular algorithm.
5. Select or implement the concrete `scorer.implementation`. Reuse a tested
   generic capability when it fits the evidence. Otherwise add a tested generic
   capability or report the task unsupported; never replace the approved
   objective with an easier observable proxy. Put the default kind and
   implementation in `games/<game>.yaml`; keep task calibration, reward,
   phase, and terminal parameters in the benchmark scenario YAML.
6. Randomly sample the human-play recording and visualize, for every selected
   frame, the observed region or evidence, raw measurement, classified phase,
   score delta, accumulated score, and terminal decision. Review the output
   with the user when semantic interpretation is still uncertain.
7. Verify the scorer against the full recording: score direction and range,
   accumulation/reset behavior, missing-signal behavior, menu and transition
   false positives, terminal decisions, temporal stability, and obvious reward
   hacking. Missing evidence must not silently become success or reward.
8. Run repeated serial cold starts and a no-LLM real-game smoke. Confirm that
   state preparation, launch, `menu_skip`, `start_check`, scoring, termination,
   closing, and the next independent episode all remain contained.
9. Preserve the human-play evidence location, visualization/offline replay
   checks, repeated cold-start results, and the user's acceptance in the
   relevant docs/tests. Do not encode trust as a generic
   `calibration_status: provisional/verified` scenario switch.
