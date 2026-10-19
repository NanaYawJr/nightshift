# Evaluation

Most agent projects are demonstrated by a video of one successful run. That shows the system can work, not how often it does. This document defines how Nightshift is measured, and the results are published including the failures.

It is written in two halves, and the division matters. **Detection results to date** are measured, on a live estate, and can be reproduced from the telemetry in the repository. **The full benchmark** is not yet run — its tables are empty and labelled as such. Nothing in the first half depends on the second being finished, and nothing in the second is implied by the first.

## What is being measured

Three questions, in descending order of how much they matter:

1. **Did it find the actual cause?** Not a plausible cause. The one that was injected.
2. **Was the proposed fix accepted unchanged by a human reviewer?**
3. **Did the estimated saving match the measured saving?**

A system that finds the right cause and proposes an unusable fix is still valuable — the engineer skips the four hours of digging. A system that proposes a clean fix for the wrong cause is worse than useless, because it is convincing.

A fourth question sits beneath all three and was learned the hard way: **was the system watching at all?** Twice during this build the collector stopped and detection went on reporting with confidence against frozen telemetry. Any accuracy figure computed over a period when collection had stalled is meaningless, so collection coverage is now measured alongside everything else (D-036).

## The fault taxonomy

Eight classes. Six were planned; two were discovered during the build and are kept because a fault found by accident is still a fault.

| Class | Planned n | Status | What is injected |
|---|---|---|---|
| `silent_zero_row` | 4 | **implemented, measured** | Source file reduced to a header; copy activity succeeds carrying nothing |
| `schema_drift` | 6 | implemented | A source column renamed, retyped or removed |
| `model_bloat` | 7 | implemented | A column widened to high cardinality inside a semantic model (D-019) |
| `refresh_collision` | 5 | pending | Background refreshes scheduled into the interactive peak |
| `contention` | 5 | pending | Two items competing for capacity, with a deliberate victim and aggressor |
| `gateway_timeout` | 3 | pending | Simulated on-premises source stalling beyond the gateway boundary |
| `unrefreshable_model` | — | discovered, not injectable | A published model bound to a connection it cannot authenticate with (D-021) |
| `collection_gap` | — | discovered, rule built | The monitoring system's own collection stops (D-036) |

`unrefreshable_model` was found in the wild, on a model that had never successfully refreshed and reported no error anywhere. `collection_gap` was found twice, on Nightshift itself.

Faults are injected on a randomised schedule over the collection window, so detection latency is measured under realistic conditions rather than immediately after injection.

## Blind replay protocol

The harness must not leak ground truth into anything downstream of detection.

1. The harness selects a fault and injects it into the synthetic estate.
2. It records the ground truth — class, target item, actual mechanism — to a sealed file the agent path cannot read.
3. Normal operation proceeds. Collectors run, detection fires, the agent investigates. Nothing in the pipeline knows this incident is synthetic except the `benchmark_ref` field, which is written after the diagnosis is complete.
4. The resulting incident is scored against the sealed record.
5. A human reviewer sees the incident exactly as they would a real one, with no indication it is a test, and approves or rejects.

Step 5 is the one people skip, and it is the only way to get an honest fix-acceptance figure. A reviewer who knows they are scoring a benchmark is a more generous reviewer.

## Scoring rubric

**Root cause** — one of three verdicts, assigned by comparing the diagnosis to the sealed record.

| Verdict | Definition |
|---|---|
| Correct | Names the injected mechanism and the correct target item |
| Partial | Names the correct item but the wrong mechanism, or the correct mechanism on the wrong item |
| Incorrect | Neither |

Partial counts as a miss in the headline figure. It is tracked separately because the two kinds of partial fail differently: right item, wrong mechanism is recoverable by an engineer in minutes; right mechanism, wrong item sends them somewhere useless.

A fourth outcome is recorded separately and is not a miss: **abstention**, where the model states the evidence is insufficient rather than guessing. An honest abstention is a usable answer. A confident wrong one is not.

**Fix acceptance** — binary, from the human decision recorded on the incident.

| Verdict | Definition |
|---|---|
| Accepted | Merged without modification |
| Modified | Merged after edit — counted as not accepted, but tracked |
| Rejected | Not merged |

**Saving accuracy** — for accepted fixes only. Absolute percentage error between the estimate at proposal time and the measured figure 48 hours after merge.

**Detection latency** — time from fault injection to detection rule firing. Reported as a distribution, not a mean; the tail is what matters.

---

# Detection results to date

Measured on the live estate, not simulated. Detection only — the agent's accuracy is reported separately below and is not yet scored.

**Observation window:** 2026-10-01 15:52 to 2026-10-15 10:46 UTC, 13.8 days.
**Subject:** `pl_ingest_ledger_daily`, hourly, writing to `ledger_daily` in `h1-finance-prod`.
**Rule:** `silent_zero_row`.

## Headline

| Metric | Result |
|---|---|
| Pipeline runs judged | 330 |
| Runs flagged | 72 |
| Runs not flagged | 258 |
| Commits that delivered rows | 258 |
| Commits that delivered nothing | 73 |
| Commits with an unknown row count | 0 |
| Typical delivery | 5,000 rows |
| False positives | 0 |

Every run the rule left alone corresponds to a commit that delivered rows. Every run it flagged corresponds to a commit that delivered none. The 258/258 correspondence is exact, which is the strongest available evidence that the rule is measuring the thing it claims to measure rather than approximating it.

## Before and after the restore

The fault was reverted at approximately 15:00 UTC on 7 October. Splitting the window there separates the period that contained faults from the period that did not.

| Period | Runs judged | Flagged |
|---|---|---|
| 1 Oct 15:52 – 7 Oct 15:00 | 143 | 72 |
| 7 Oct 15:00 – 15 Oct 10:46 | 187 | **0** |

187 consecutive healthy runs over almost eight days, none flagged. That is the figure that makes the other one worth anything: a rule that fires on faults is easy, and a rule that stays quiet for eight days while a pipeline runs normally is the half most demonstrations never show.

## The fault windows

Each injection appears as a block of consecutive silent runs. The boundaries were not given to the rule; they are where it stopped and started firing.

| Day | Silent runs | First | Last |
|---|---|---|---|
| 2 Oct | 4 | 12:46 | 15:46 |
| 4 Oct | 6 | 18:46 | 23:46 |
| 5 Oct | 23 | 00:46 | 23:46 |
| 6 Oct | 24 | 00:46 | 23:46 |
| 7 Oct | 15 | 00:46 | 14:46 |

Three injection episodes. The first was reverted manually after four hours. The second ran from the evening of 4 October until it was accidentally restored on 5 October — visible as the single healthy run in a day that otherwise has 23 silent ones out of 24. The third ran from that evening until the deliberate restore on 7 October.

3 October contains no incidents. The estate was healthy that day and the rule said so.

Every boundary matches an injection or a restore to the hour, and none of those times were available to the rule.

## Open discrepancy

73 commits delivered nothing; 72 runs were flagged. One zero-row commit has no matching incident.

The likely explanation is that its run falls outside the evaluation cutoff — the rule deliberately refuses to judge runs whose commits may not have been collected yet. **This has not been confirmed.** It is recorded here rather than rounded away, because an unexplained gap of one is how an unexplained gap of forty begins.

## Controlled injection, 15 October

The first injection made under measurement conditions: sealed ground truth, a
recorded injection time, and source-file sampling running throughout. Everything
before this was reconstructed after the fact.

| | |
|---|---|
| Injected | 15 Oct 15:03:17 UTC |
| Fault | `silent_zero_row`, sealed as `silent_zero_row-pl_ingest_ledger_daily-2cf897-4417` |
| Mechanism | Landing file rewritten with its header row and no data rows |
| First affected run | 15:46:04 |
| First collection carrying the evidence | 16:04:30 |
| **Detection latency** | **61 minutes** |
| Silent runs detected | 4 (15:46, 16:46, 17:46, 18:46) |
| False positives in the same window | 0 |

Almost all of the 61 minutes is schedule, not work: the pipeline runs hourly and
the collector runs hourly, so a fault injected just after a run waits for the
next one to manifest and the one after that to be observed. The rule itself
fires on the first collection that contains the evidence. Reported as measured
rather than adjusted, because an hourly estate genuinely does take about an hour
to notice this.

### What the source sampler recorded

The first time the project has measured the thing that actually broke, rather
than the symptoms downstream of it.

| Sampled | Exists | Data rows | Size |
|---|---|---|---|
| 14:20:21 | yes | 5,000 | 604,780 |
| 15:03:38 | yes | **0** | **128** |
| 16:04:30 | yes | 0 | 128 |
| 17:05:16 | yes | 0 | 128 |
| 18:05:31 | yes | 0 | 128 |
| 19:05:51 | yes | 0 | 128 |

Twenty-one seconds after injection the file is 128 bytes and empty, and it stays
that way. Note what does **not** change: the file is present, readable and
recently modified throughout. Every property except the row count reads as
healthy, which is the whole fault in one table and the reason `source.data_rows`
had to be collected at all.

## Agent result, scored

One incident from this window, diagnosed and compared to the sealed record.

**Incident:** `INC-b5bedd44bf`, the 18:46 run.

**Sealed mechanism:** *the landing file feeding `pl_ingest_ledger_daily` was
written with its header row and no data rows.*

**Diagnosis:** *the pipeline completed successfully but delivered zero rows
because the source file contained no data rows.* Cited `run.status`,
`run.rows_delivered`, `expectation.minimum_rows`, `source.exists`,
`source.data_rows`. Recommended checking the upstream process that writes the
file.

| | |
|---|---|
| Root cause | **Correct** — right mechanism, right item |
| Confidence | high |
| Citations valid | 5 of 5 |
| Source | model |

Against the same fault class a week earlier, with no source facts in the pack,
the same model produced four confident restatements of the symptom and two
abstentions, and reached no cause at all. The model did not change. The evidence
did.

### Two caveats, stated rather than enjoyed

**The pack sits close to the answer.** "Source had 0 rows" plus "run delivered
0 rows" plus "run completed" is a short step to the conclusion. It is a real
inference — three facts joined, and a judgement that the pipeline is innocent —
but a sceptical reader is entitled to call it a small one.

**The negative control has not been run.** The risk of adding source facts is
that the model now blames the source for everything. The test is a fault where
the source is healthy and delivery is still zero. Until that runs, this result
is consistent with both genuine reasoning and pattern-matching, and it would be
dishonest to claim otherwise.

## What is not measured

**Detection latency.** The injections were manual and detection was not running continuously during them, so time-to-detect cannot be computed from this data. It requires the harness.

**Recall against ground truth beyond these three episodes.** Three injections of one fault class is not a sample. The windows match the sealed records exactly, which is necessary but nowhere near sufficient.

**Anything about the other seven fault classes.** `schema_drift` and `model_bloat` are implemented and verified as injectable and revertible, but no detection rule exists for either yet.

---

# Agent results

Not yet scored. The diagnostician runs end to end and its first output is recorded here because it is informative, not because it is a result.

## First run — 6 incidents, `gemini-3.5-flash-lite`

Google Gemini free tier, via the OpenAI-compatible endpoint. Temperature 0,
one call per incident, no tool-calling loop.

| Outcome | n |
|---|---|
| Diagnosis produced, citations valid | 4 |
| Abstained — evidence insufficient | 2 |
| Fell back — model unreachable or unparseable | 0 |
| **Citations that referenced a fact not in the evidence pack** | **0** |

The citation check held on every attempt. Not one diagnosis rested on a number that had not been measured, which is the specific failure this layer exists to prevent.

**None of the four reached a cause.** All four restated the incident — "the pipeline completed but delivered zero rows" — and recommended checking the source. Fluent, high-confidence, and carrying no information the deterministic rule had not already produced.

The two abstentions were better work. One noticed that 73 of 148 commits had added no rows and reasoned that zero might be normal for this table before declining to conclude. Wrong, as it happens — those 73 are the injected fault — but it is reasoning about a baseline, which the confident four never attempted.

## Why, and what it implies

The evidence pack contains nothing about the source. No landing file size, no row count, no modified time. The model was asked why nothing arrived while being shown only the mechanism that does the carrying, so "check the source" is the correct next step given what it holds — it correctly located the edge of its own knowledge.

**A diagnosis can only be as specific as the evidence pack's coverage.** That is a more useful finding than a poorly performing model would have been, and it points at a concrete next step: a source-side collector. It is also an argument against the reflex of tuning the prompt until the answer looks right, which would have produced a confident cause with no evidence behind it.

---

# The full benchmark

Not yet run. Populated by `python -m eval.harness --faults 30 --blind`. Raw output in `eval/results/`.

| Metric | Result |
|---|---|
| Faults injected | — |
| Root cause correct | — |
| Root cause partial | — |
| Root cause incorrect | — |
| Abstentions | — |
| Fix accepted unchanged | — |
| Median detection latency | — |
| 90th percentile detection latency | — |
| Mean saving estimate error | — |
| Incidents produced by fallback path | — |

| Class | Injected | Correct | Partial | Incorrect | Fix accepted |
|---|---|---|---|---|---|
| `silent_zero_row` | — | — | — | — | — |
| `schema_drift` | — | — | — | — | — |
| `model_bloat` | — | — | — | — | — |
| `refresh_collision` | — | — | — | — | — |
| `contention` | — | — | — | — | — |
| `gateway_timeout` | — | — | — | — | — |

## Known weaknesses

Written before the run, because predicting where a system will fail is part of understanding it. Each will be confirmed or corrected against the results.

**The evidence pack is the binding constraint, not the model.** Confirmed early: see the agent results above. Until a collector supplies source-side facts, no diagnosis of `silent_zero_row` can be more specific than "look upstream".

**The landing file is static between injections.** A real landing area receives
a new file every hour; this one is rewritten only when a fault or a restore
touches it. So `source.modified_at` reports when the estate was last interfered
with, not anything about normal operation, and `source.hours_old_at_run` reads
as 192 hours on a perfectly healthy run. A model could reasonably mistake that
for staleness. Not fixed during the measurement window, deliberately — changing
the estate partway through invalidates the comparison with the runs already
recorded.

**Source facts are accurate only to within one collection interval.** The
evidence pack uses the last sample taken at or before the run, so a fault
injected between samples leaves the first incident describing a file that has
since changed. In the 15 October run a sample happened to land 21 seconds after
injection and all four incidents were unaffected, but that was luck. The fix is
either to sample more often than the pipeline runs, or to put the sample's age
in the pack so the model can discount it.

**Contention is expected to be the worst class.** When two items compete for capacity, the telemetry shows the victim clearly — long duration, high consumption — and the aggressor barely at all, because it finished quickly by winning. Ranking by consumption therefore names the victim. Consumption is not causation when items are queuing for the same resource, and the variance engine has no way to know the difference. Resolving it needs queue-wait telemetry the trial capacity does not expose.

**Gateway faults are visible only as symptoms.** Anything originating beyond the gateway boundary presents as an unexplained stall. The agent can correctly identify that the cause is upstream and outside its visibility, which is arguably the right answer, but it scores as a miss under this rubric. Both readings are reported.

**Model bloat fixes are expected to be over-aggressive.** The diagnosis should be reliable; the proposal will likely remove more than a reviewer wants removed. Expect high root-cause accuracy and lower fix acceptance in this class.

**Single-reviewer bias.** One person makes every accept/reject call, so fix acceptance measures agreement with one engineer's judgement rather than a general standard. Stated as a limitation rather than corrected, since a second reviewer is not available on this project.

**One estate, one tenant, trial capacity.** Every figure here comes from a single synthetic estate on a 60-day trial. Nothing has been tested against a real production estate, against a second tenant, or against sustained capacity pressure.

## Reproducing

Working today:

```bash
python -m pytest -q                     # 233 tests
python -m estate.seed.generate --seed 4417
```

Detection and diagnosis run as scheduled Fabric notebooks in `h1-nightshift`:
`10_collect` hourly, then `20_detect` and `30_diagnose` against the telemetry
it writes. The notebooks fetch this repository from `main` at run time, so the
commit they ran against is whatever `main` held at that moment.

Not yet built:

```bash
python -m estate.deploy --tenant <id> --capacity <id>
python -m eval.harness --faults 30 --blind --out eval/results/
python -m eval.report --in eval/results/ --out docs/EVALUATION.md
```

The harness is intended to be deterministic given a seed, so that `--seed 4417` reproduces a published run exactly, including which faults were injected when.