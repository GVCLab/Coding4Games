---
name: analyzing-windows-benchmark-reports
description: Analyze existing Windows benchmark development runs, independent tests, and Direct baselines using host evidence and artifact identity.
---

# Analyze Windows benchmark runs

Read the [evidence contract](../../../docs/contracts/EVIDENCE_AND_ACCEPTANCE.md).
Work offline with the requested archives. Stream gateway and model-call logs
line by line and redact credentials, private paths, and raw image payloads.

1. Start with each run's `config.json`. Record the scenario, input protocol,
   CLI/model, phase, baseline/profile identity, limits, and actual resource use.
2. Join host episode records by episode UID. Follow `programs/final.json` and the
   immutable program manifest to identify the selected code. Group independent
   repeats by program ID. Track changes in development separately from repeats
   of an identical bundle. Inspect `development_timeline.jsonl` and the gateway
   audit for the sequence of tool calls, evaluations, and selection.
3. Keep valid scores, incomplete tasks, infrastructure errors, and
   budget-limited exposure visible. Preserve null scores and unknown token use.
   Compare native Direct with stepped runs only where the stated metric and
   protocol support the comparison.
4. Report the selected program's independent results with the sample count,
   actual tics and wall time, and links to the relevant evidence. Report
   model calls and tokens by phase. Derive plots from these records and label
   incomplete runs and materially different exposure.
5. Tie a failure diagnosis to a specific observable event. Treat perception,
   planning, input delivery, timing, and startup as separate hypotheses when
   the evidence cannot distinguish them.

For archived Code/Lang analyses, use the original checkout's report tools
and schema. Join policy history by content SHA and keep the frozen submission,
adaptive evaluation repeats, and independent reruns distinct. The previous
summarizer and report method are available at Git commit
`21b6e0dfdb8859b89f8f9f1c7c9693ceea842168`.
