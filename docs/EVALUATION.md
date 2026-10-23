# Evaluation

Most agent projects are demonstrated by a video of one successful run. That shows the system can work, not how often it does. This document defines how Nightshift is measured, and publishes the results including the failures.

One fault class has now been taken end to end under measurement conditions — detected, diagnosed, scored against sealed ground truth, and reviewed by a human — and its results are below. Seven other classes are not measured, and nothing here should be read as evidence about them.

Two numbers in this document are zero and are meant to be. One of them is the false-positive count. The other is the number of proposed fixes a human accepted.

## What is being measured

Three questions, in descending order of how much they matter:

1. **Did it find the actual cause?** Not a plausible cause. The one that was injected.
2. **Was the proposed fix accepted unchanged by a human reviewer?**
3. **Did the estimated saving match the measured saving?**

A system that finds the right cause and proposes an unusable fix is still valuable — the engineer skips the four hours of digging. A system that proposes a clean fix for the wrong cause is worse than useless, because it is convincing.

Question 3 is not measured in this document. No fix has been accepted, so no saving has been realised to compare an estimate against.

A fourth question sits beneath all three and was learned the hard way: **was the system watching at all?** Twice during this build the collector stopped and detection went on reporting with confidence against frozen telemetry. Any accuracy figure computed over a period when collection had stalled is meaningless, so collection coverage is measured alongside everything else (D-036).

## The fault taxonomy

Eight classes. Six were planned; two were discovered during the build and are kept because a fault found by accident is still a fault.

| Class | Planned n | Status | What is injected |
|---|---|---|---|
| `silent_zero_row` | 4 | **measured end to end** | Source file reduced to a header; copy activity succeeds carrying nothing |
| `schema_drift` | 6 | injectable, no rule | A source column renamed, retyped or removed |
| `model_bloat` | 7 | injectable, no rule | A column widened to high cardinality inside a semantic model (D-019) |
| `refresh_collision` | 5 | pending | Background refreshes scheduled into the interactive peak |
| `contention` | 5 | pending | Two items competing for capacity, with a deliberate victim and aggressor |
| `gateway_timeout` | 3 | pending | Simulated on-premises source stalling beyond the gateway boundary |
| `unrefreshable_model` | — | discovered, not injectable | A published model bound to a connection it cannot authenticate with (D-021) |
| `collection_gap` | — | discovered, rule built | The monitoring system's own collection stops (D-036) |

`unrefreshable_model` was found in the wild, on a model that had never successfully refreshed and reported no error anywhere. `collection_gap` was found twice, on Nightshift itself.

## Blind replay protocol

The harness must not leak ground truth into anything downstream of detection.

1. A fault is injected into the estate by hand, from a scratch notebook.
2. Ground truth — class, target item, mechanism, injection time — is written to a sealed file at the moment of injection. No module under `collectors/`, `detection/` or `agent/` imports from `estate.faults`, and the diagnosis notebooks do not open the seal.
3. Normal operation proceeds. Collectors run hourly, detection fires, the agent investigates. Nothing in that path knows the incident is synthetic.
4. The fault is reverted and the end time recorded, closing the window.
5. The harness scores the resulting incidents against the sealed record.
6. A human reviewer sees the incident as they would a real one and approves or rejects the proposed fix.

Injection is manual on purpose. An unattended injector would add ways to go wrong without adding anything a reader should trust more — the property that makes the result credible is the seal, not the automation.

### Corrections to a sealed record

A sealed record exists to stop ground truth being adjusted after the results are known. It does not exist to preserve a value that is demonstrably wrong.

The first benchmark run recorded this fault as ending on 20 October. It ended on 15 October; 20 October was the clock reading when the revert was *recorded*, four days after the revert was *performed*. The estate's own telemetry contradicted the seal.

The record was therefore corrected, and the correction is part of the record: the superseded value, the evidence, and the time of the correction are stored in the file alongside the new value, and nothing is erased (D-046). A correction with no stated evidence is refused by the code. The corrected end time was derived from the hourly source samples, and because no pipeline run falls between the last silent sample and the first healthy one, the correction changes no score — attribution, latency and verdicts are identical before and after.

The cause has also been closed: the method that records an end time no longer has access to a default. It must be told when the fault ended.

## Scoring rubric

**Root cause** — assigned by a human comparing the diagnosis to the sealed record. No string comparison is used: *"the source file contained no data rows"* and *"the landing file was written with its header row and no data rows"* are the same answer in different words, and keyword matching would score this project's vocabulary rather than its correctness.

| Verdict | Definition |
|---|---|
| Correct | Names the injected mechanism and the correct target item |
| Partial | The correct item with the wrong mechanism, or the correct mechanism on the wrong item |
| **Restatement** | Restates the symptom as the cause. Names no mechanism at all |
| Incorrect | A different cause |
| Abstained | Declined to name a cause, stating the evidence was insufficient |

`Restatement` was added after the fact (D-044). The original three verdicts had nowhere to put a diagnosis reading *"the pipeline completed successfully but delivered zero rows"* for an incident that **is** the pipeline completing successfully and delivering zero rows. It is not a wrong mechanism — it is no mechanism, carrying nothing the deterministic detection rule had not already produced. It is also the failure mode that looks most like success, being fluent, specific and confident, which is why it needed a name of its own rather than being folded into "partial".

`Abstained` is not a miss. A system that says "I do not know" when it does not know is behaving correctly, and scoring that as failure would reward confident guessing — which is precisely the behaviour `restatement` exists to catch.

**Verdicts are reported separately for incidents inside a sealed fault window and incidents outside one** (D-047). Only the first group can be scored for correctness, because only there is there something to compare against. An incident outside every window can still be scored for restatement — "names no mechanism" is checkable from the text alone — but pooling the two groups would let six unsealed judgements dilute five sealed ones into a single meaningless ratio.

**Fix acceptance** — binary, from the human decision recorded on the incident. The question is *"would you merge this unmodified"*, not *"is this roughly right"*.

| Verdict | Definition |
|---|---|
| Accepted | Would be merged without modification |
| Rejected | Would not. The note records what would have had to change |

**Applying a fix is out of scope.** Nightshift proposes changes; it does not raise pull requests, edit item definitions or alter the estate. Approving a proposal records a judgement and changes nothing. A self-healing system that writes to production without a human in the loop is a different project with a different risk profile, and claiming this one does that would be false.

**Detection latency** — reported as two distinct measurements, because they answer different questions and the difference is mostly schedule rather than work.

| Measurement | From | To |
|---|---|---|
| Time to manifest | Injection | Start of the first affected pipeline run |
| Time to evidence | Injection | First collection carrying the evidence |

---

# Results

Measured on a live Fabric trial estate. Reproducible from the telemetry in the Lakehouse and the sealed record in `lh_nightshift/Files/sealed`.

## The benchmark table

As produced by `eval.harness.results()` on 23 October 2026.

| | |
|---|---|
| Faults sealed | 1 |
| Faults detected | 1 |
| Faults missed | 0 |
| Faults still live | 0 |
| Detection latency, median | 0.71 h |
| Detection latency, worst | 0.71 h |
| Incidents in the store | 77 |
| Incidents inside a fault window | 5 |
| Incidents outside every window | 72 |
| Diagnoses produced | 11 |
| — from the model | 8 |
| — abstentions | 3 |
| — from the degraded fallback path | 0 |
| **Scored against the seal** | **5** |
| — correct | **5** |
| — partial, restatement, incorrect | 0 |
| Scored without a seal | 6 |
| — restatement | 3 |
| — abstained | 3 |
| Proposals produced | 11 |
| — naming a pipeline change | 8 |
| — declining to propose | 3 |
| **Fixes accepted unchanged** | **0 of 8** |
| Incidents never diagnosed | 66 |

The 72 incidents outside every window are not false positives. They are the seeded historical estate — real zero-row deliveries the rule correctly flagged, for which no sealed ground truth exists because they were not injected under measurement conditions. Reporting them as false positives would score the rule as wrong for being right.

## Detection

**Window:** 1 Oct 15:52 to 15 Oct 10:46 UTC, 13.8 days.
**Subject:** `pl_ingest_ledger_daily`, hourly, writing to `ledger_daily` in `h1-finance-prod`.

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

Every run the rule left alone corresponds to a commit that delivered rows. Every run it flagged corresponds to a commit that delivered none. The 258/258 correspondence is exact, which is the strongest available evidence that the rule measures the thing it claims to measure rather than approximating it.

### The quiet period

| Period | Runs judged | Flagged |
|---|---|---|
| 1 Oct 15:52 – 7 Oct 15:00 | 143 | 72 |
| 7 Oct 15:00 – 15 Oct 10:46 | 187 | **0** |

187 consecutive healthy runs over almost eight days, none flagged. A rule that fires on faults is easy. A rule that stays quiet for eight days while a pipeline runs normally is the half most demonstrations never show.

A second quiet period has since run unattended: between the restore on 15 October and 23 October, a further eight days, the incident count did not move. Collection was continuous throughout — confirmed from the collector's own timestamps, not assumed.

### Open discrepancy

73 commits delivered nothing; 72 runs were flagged. One zero-row commit has no matching incident.

The likely explanation is that its run falls outside the evaluation cutoff — the rule deliberately refuses to judge runs whose commits may not have been collected yet. **This has not been confirmed.** It is recorded here rather than rounded away, because an unexplained gap of one is how an unexplained gap of forty begins.

## Controlled injection, 15 October

The first injection made under full measurement conditions: sealed ground truth, a recorded injection time, and source-file sampling running throughout.

| | |
|---|---|
| Fault | `silent_zero_row-pl_ingest_ledger_daily-2cf897-4417` |
| Mechanism (sealed) | The landing file was written with its header row and no data rows |
| Injected | 15 Oct 15:03:17 UTC |
| Reverted | 15 Oct 20:03:18 UTC (corrected — see *Corrections to a sealed record*) |
| Window length | 5h 00m |
| First affected run | 15:46:04 |
| First collection carrying the evidence | 16:04:30 |
| **Time to manifest** | **0.71 h (43 min)** |
| **Time to evidence** | **1.02 h (61 min)** |
| Silent runs detected | 5 — 15:46, 16:46, 17:46, 18:46, 19:46 |
| Silent runs missed | 0 |
| False positives in the window | 0 |

Almost all of that is schedule, not work. The pipeline runs hourly and the collector runs hourly, so a fault injected just after a run waits for the next run to manifest and the collection after that to be observed. The rule fires on the first collection containing the evidence. Reported as measured rather than adjusted, because an hourly estate genuinely does take about an hour to notice this.

### What the source sampler recorded

| Sampled | Exists | Data rows | Size (bytes) |
|---|---|---|---|
| 14:20:21 | yes | 5,000 | 604,780 |
| 15:03:38 | yes | **0** | **128** |
| 16:04:30 | yes | 0 | 128 |
| 17:05:16 | yes | 0 | 128 |
| 18:05:31 | yes | 0 | 128 |
| 19:05:51 | yes | 0 | 128 |
| 20:03:18 | yes | **5,000** | 604,780 |

Twenty-one seconds after injection the file is 128 bytes and empty, and it stays that way for five hours. Note what does **not** change: the file is present, readable and recently modified throughout. Every property except the row count reads as healthy. That is the whole fault in one table, and the reason `source.data_rows` had to be collected at all.

The final row is also the evidence the sealed end time was corrected from.

## Diagnosis

Model: `gemini-3.5-flash-lite`, Google free tier via the OpenAI-compatible endpoint. Temperature 0, one call per incident, no tool-calling loop.

**Five incidents inside the fault window, with source samples in the evidence pack.**

| Verdict | n |
|---|---|
| Correct | **5** |
| Partial | 0 |
| Restatement | 0 |
| Incorrect | 0 |
| Abstained | 0 |
| Citations referencing a fact not in the pack | **0** |

All five named the mechanism. All five stated, in the reasoning, that the source file *existed* and contained zero rows — which is the distinction between an empty file and a missing one, and the distinction that decides which upstream problem an engineer goes looking for.

**Six incidents from before the source sampler existed, with no source facts available.**

| Verdict | n |
|---|---|
| Restatement | **3** |
| Abstained | **3** |
| Correct, partial, incorrect | — not scoreable, no sealed ground truth |

Two of the three restatements explicitly say *"the root cause cannot be determined from the source side"* — and then fill in a cause anyway, by rephrasing the symptom. The three abstentions, given the same evidence and the same prompt, declined.

### The finding

| Source evidence in the pack | Mechanism named | Abstained | Symptom restated as cause |
|---|---|---|---|
| No (6 incidents) | 0 | 3 | 3 |
| Yes (5 incidents) | 5 | 0 | 0 |

Same model, same prompt, same temperature, same fault class. The restatement failure was not a reasoning failure — it was an evidence failure, and the system's behaviour when the evidence is absent is to abstain roughly half the time and confidently rephrase the problem the rest.

**A diagnosis can only be as specific as the evidence pack's coverage**, and the honest failure mode of an insufficient pack is not silence. It is fluent, high-confidence restatement that passes every structural check: valid JSON, valid citations, a stated confidence, a recommended action. Nothing in the output marks it as empty. Only comparison against ground truth does.

This is the single most useful thing the project has measured, and it is only visible because abstention, restatement and model degradation are recorded as three different outcomes rather than one (D-038, D-039, D-044). Collapsed into a success rate, the entire finding disappears.

### Temperature 0 is not reproducible

Two runs of the same six incidents, same prompt, same temperature, produced **1** model-sourced diagnosis and **3** respectively, with the remainder abstaining. A third run before the prompt was corrected produced 4.

The deterministic layers of this system — collection, detection, scoring, attribution — are reproducible from a seed. **The model layer is not**, and an earlier version of this document claimed otherwise. Any published figure that depends on a model call is a sample of one draw, and the sample size here is small enough that the correct reading of "3 restatements out of 6" is *"this happens often"*, not *"this happens 50% of the time"*.

## Remediation

| | |
|---|---|
| Proposals naming a change | 8 |
| Proposals declining to propose | 3 |
| **Accepted unchanged by a human** | **0** |
| Rejected | 8 |

The five proposals written with full source evidence are directionally correct, internally consistent, and state their own risks — each one notes that a legitimately empty source day would now fail loudly. All five were rejected, for the same reason, and it is structural.

Every one of them says: *check `source.data_rows` against `expectation.minimum_rows` and fail the run*. Both of those are **Nightshift's own fact keys**. `source.data_rows` is something the landing sampler measured; `expectation.minimum_rows` is a value in a `WriteExpectation` config object. Neither exists anywhere inside the pipeline. An engineer would have to translate the proposal into a Get Metadata activity, an If Condition and a Fail activity — which is the actual work, and the agent did not do it.

**The agent is forced into this.** `agent.proposal.validate` rejects any proposal citing a fact key absent from the evidence pack — the guard that stops it inventing plausible-sounding telemetry. But the evidence pack contains only Nightshift's measurements, so the only vocabulary available for writing a fix is the vocabulary of the measurements. The mechanism that prevents hallucination is the mechanism that prevents implementability. They are the same constraint in opposite directions.

No prompt change resolves this. The agent has never been shown what a pipeline is made of — it has seen that `pl_ingest_ledger_daily` ran for 22 seconds and wrote nothing, and nothing about the activities inside it. A diff cannot be written against an object that has never been observed.

The fix is an item-definition collector, which is not built. Until it is, the honest claim is: **Nightshift can tell you what broke and cannot yet tell you what to change.**

A secondary observation: one fault produced five near-identical proposals, one per affected run, at two model calls each. Consistency is reassuring; ten calls to answer one question is not. Incident-level remediation re-derives the same fix per occurrence, and grouping by fault is an obvious improvement that has not been made.

---

# What this does not show

**The negative control has not been run.** Adding source facts to the pack risks the model now blaming the source for everything. The test is a fault where the source is healthy and delivery is still zero. Until that runs, the 5-for-5 diagnosis result is consistent with both genuine reasoning and pattern-matching, and it would be dishonest to claim otherwise. This is the single largest gap in the result.

**The pack sits close to the answer.** "Source had 0 rows" plus "run delivered 0 rows" plus "run completed normally" is a short step to the conclusion. It is a real inference — three facts joined, plus a judgement that the pipeline is innocent — but a sceptical reader is entitled to call it a small one.

**One fault, five incidents, one class.** Five correct diagnoses of five occurrences of a single injected fault is one data point, not a sample. The seven other fault classes are unmeasured, and two of them have no detection rule at all.

**Single reviewer.** One person makes every accept/reject and every verdict call, so both figures measure agreement with one engineer's judgement rather than a general standard. Stated as a limitation rather than corrected, since a second reviewer is not available on this project.

**One estate, one tenant, trial capacity.** Every figure comes from a single synthetic estate on a 60-day Fabric trial. Nothing has been tested against a production estate, a second tenant, or sustained capacity pressure. There is no Key Vault on trial capacity, so the model credential is a file in OneLake rather than a managed secret — a documented deviation, not a design.

**The landing file is static between injections.** A real landing area receives a new file every hour; this one is rewritten only when a fault or a restore touches it. So `source.modified_at` reports when the estate was last interfered with, and `source.hours_old_at_run` can read as many days on a perfectly healthy run. A model could reasonably mistake that for staleness. Not fixed during the measurement window, deliberately — changing the estate partway through invalidates comparison with the runs already recorded.

**Source facts are accurate only to within one collection interval.** The pack uses the last sample taken at or before the run. A fault injected between samples leaves the first incident describing a file that has since changed. On 15 October a sample landed 21 seconds after injection and no incident was affected — that was luck, not design.

## Predicted weaknesses in the unmeasured classes

Written in advance, because predicting where a system will fail is part of understanding it. Each will be confirmed or corrected if those classes are ever run.

**Contention is expected to be the worst class.** When two items compete for capacity, the telemetry shows the victim clearly — long duration, high consumption — and the aggressor barely at all, because it finished quickly by winning. Ranking by consumption therefore names the victim. Consumption is not causation when items queue for the same resource, and resolving it needs queue-wait telemetry the trial capacity does not expose.

**Gateway faults are visible only as symptoms.** Anything originating beyond the gateway boundary presents as an unexplained stall. The agent can correctly identify that the cause is upstream and outside its visibility, which is arguably the right answer, but it scores as a miss under this rubric. Both readings would be reported.

**Model bloat fixes are expected to be over-aggressive.** The diagnosis should be reliable; the proposal will likely remove more than a reviewer wants removed. Expect high root-cause accuracy and lower fix acceptance.

---

# Reproducing

```bash
python -m pytest -q                        # 315 tests
python -m estate.seed.generate --seed 4417
```

Collection, detection, diagnosis and review run as Fabric notebooks in `h1-nightshift` against `lh_nightshift`: `10_collect` hourly, then `20_detect`, `30_diagnose` and `40_review`. The notebooks fetch this repository from `main` at run time, so the commit they ran against is whatever `main` held at that moment.

The scoring table is produced by:

```python
from estate.faults.base import Seal
from eval import harness

seal = Seal("/lakehouse/default/Files/sealed")
print(harness.table(seal=seal))
print(harness.results(seal=seal))
```

Both refuse to report anything when the seal holds no faults, rather than returning a table of zeros (D-045). Both state which storage backend and which seal path produced the figures, because an unconfigured run and a run that found nothing otherwise produce identical output.

Not built:

```bash
python -m estate.deploy --tenant <id> --capacity <id>
python -m eval.report --in eval/results/ --out docs/EVALUATION.md
```

Deterministic given a seed: collection, detection, incident identity, attribution and scoring. **Not deterministic:** anything that depends on a model call. See *Temperature 0 is not reproducible* above.