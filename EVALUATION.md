# Evaluation

Most agent projects are demonstrated by a video of one successful run. That shows the system can work, not how often it does. This document defines how Nightshift is measured, and the results are published including the failures.

## What is being measured

Three questions, in descending order of how much they matter:

1. **Did it find the actual cause?** Not a plausible cause. The one that was injected.
2. **Was the proposed fix accepted unchanged by a human reviewer?**
3. **Did the estimated saving match the measured saving?**

A system that finds the right cause and proposes an unusable fix is still valuable — the engineer skips the four hours of digging. A system that proposes a clean fix for the wrong cause is worse than useless, because it is convincing.

## The fault taxonomy

Six classes, thirty injected faults. Each class is a real failure pattern seen in production Fabric estates, and each has a module in `estate/faults/`.

| Class | n | What is injected |
|---|---|---|
| `schema_drift` | 6 | A source column renamed, retyped or removed |
| `model_bloat` | 7 | High-cardinality column pulled into a semantic model; inefficient DAX evaluated per row |
| `refresh_collision` | 5 | Background refreshes scheduled into the interactive peak |
| `silent_zero_row` | 4 | Copy activity set to skip incompatible rows, source shape changed |
| `contention` | 5 | Two items competing for capacity, with a deliberate victim and aggressor |
| `gateway_timeout` | 3 | Simulated on-premises source stalling beyond the gateway boundary |

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

**Fix acceptance** — binary, from the human decision recorded on the incident.

| Verdict | Definition |
|---|---|
| Accepted | Merged without modification |
| Modified | Merged after edit — counted as not accepted, but tracked |
| Rejected | Not merged |

**Saving accuracy** — for accepted fixes only. Absolute percentage error between the estimate at proposal time and the measured figure 48 hours after merge.

**Detection latency** — time from fault injection to detection rule firing. Reported as a distribution, not a mean; the tail is what matters.

## Results

Populated by `python -m eval.harness --faults 30 --blind`. Raw output in `eval/results/`.

### Headline

| Metric | Result |
|---|---|
| Faults injected | 30 |
| Root cause correct | — |
| Root cause partial | — |
| Root cause incorrect | — |
| Fix accepted unchanged | — |
| Median detection latency | — |
| 90th percentile detection latency | — |
| Mean saving estimate error | — |
| Incidents produced by fallback path | — |

### By fault class

| Class | Injected | Correct | Partial | Incorrect | Fix accepted |
|---|---|---|---|---|---|
| `schema_drift` | 6 | — | — | — | — |
| `model_bloat` | 7 | — | — | — | — |
| `refresh_collision` | 5 | — | — | — | — |
| `silent_zero_row` | 4 | — | — | — | — |
| `contention` | 5 | — | — | — | — |
| `gateway_timeout` | 3 | — | — | — | — |

## Known weaknesses

Written before the run, because predicting where a system will fail is part of understanding it. Each will be confirmed or corrected against the results.

**Contention is expected to be the worst class.** When two items compete for capacity, the telemetry shows the victim clearly — long duration, high consumption — and the aggressor barely at all, because it finished quickly by winning. Ranking by consumption therefore names the victim. Consumption is not causation when items are queuing for the same resource, and the variance engine has no way to know the difference. Resolving it needs queue-wait telemetry the trial capacity does not expose.

**Gateway faults are visible only as symptoms.** Anything originating beyond the gateway boundary presents as an unexplained stall. The agent can correctly identify that the cause is upstream and outside its visibility, which is arguably the right answer, but it scores as a miss under this rubric. Both readings are reported.

**Model bloat fixes are expected to be over-aggressive.** The diagnosis should be reliable; the proposal will likely remove more than a reviewer wants removed. Expect high root-cause accuracy and lower fix acceptance in this class.

**Single-reviewer bias.** One person makes every accept/reject call, so fix acceptance measures agreement with one engineer's judgement rather than a general standard. Stated as a limitation rather than corrected, since a second reviewer is not available on this project.

## Reproducing

```bash
python -m estate.deploy --tenant <id> --capacity <id>
python -m estate.seed --days 30
python -m eval.harness --faults 30 --blind --out eval/results/
python -m eval.report --in eval/results/ --out docs/EVALUATION.md
```

The harness is deterministic given a seed. `--seed 4417` reproduces the published run exactly, including which faults were injected when.
