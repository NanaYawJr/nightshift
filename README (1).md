# Nightshift

A platform reliability agent for Microsoft Fabric. It watches a Fabric estate, detects reliability and capacity incidents, investigates them, and proposes a fix as a pull request for a human to approve.

**Status:** in development. Built entirely on a 60-day Fabric trial capacity. Benchmark results below are populated as the evaluation harness runs.

---

## The problem

Fabric capacity is bought as a fixed monthly SKU. When an estate throttles, the default response is to buy a larger SKU — a permanent cost increase applied to what is usually a fixable scheduling collision or an unused high-cardinality column. Nobody diagnoses it because diagnosis means an engineer combing through refresh logs, VertiPaq stats and query traces by hand.

Two failure modes drive most of it:

**Loud failures** are already handled. A pipeline errors, an alert fires, someone fixes it.

**Quiet failures** are not. A source renames a column, a copy activity set to skip incompatible rows discards every row, the pipeline reports success, and two reports serve stale figures for three hours without any error state. Fabric considers nothing to have failed, because by its own definition nothing did.

Nightshift is built for the second kind, and for the slow cost drift nobody attributes to a specific item.

## What it does

1. **Collects** platform telemetry from the Fabric REST APIs, Capacity Metrics, refresh history and `semantic-link-labs` into an Eventhouse.
2. **Detects** anomalies deterministically — KQL rules and a PySpark variance engine that decomposes a duration or CU spike into the workspace, item and operation responsible.
3. **Investigates** the isolated result with a small agent loop that calls read-only tools to gather evidence: VertiPaq stats, job history, lineage, DAX dependency scans, Best Practice Analyzer.
4. **Proposes** a change as a TMDL or JSON diff, with the evidence chain and an estimated saving attached.
5. **Waits.** Every path ends at a human decision. Nothing is applied automatically.
6. **Records** everything — detection, evidence, diagnosis, proposal, decision, and the measured outcome — into a Delta incident store that doubles as the audit trail and the evaluation dataset.

## The constraint

This is built on a Fabric trial capacity, which switches off every ready-made AI feature: Copilot, Fabric data agents, AI functions and AI services are all unsupported on trial. Private Link is disabled. The capacity cannot be paused, and cannot be reset once its usage caps are hit.

That constraint shaped the project rather than limiting it. The agent loop is hand-built with the open-source Microsoft Agent Framework running inside a Fabric notebook, calling a model over the public internet. No part of the reasoning layer depends on a Fabric AI feature, which means it is portable off Fabric and not pinned to a model version.

Two operating rules follow from the trial:

- No sub-hourly schedules, capped fault-injector volume, hard iteration limits on every agent loop. A runaway retry loop burns capacity units as fast as it burns tokens, and there is no reset.
- Everything must be in git and captured on video before day 56. When the trial ends, non-Power BI items go inactive and OneLake content is only recoverable for 7 days.

## Design principles

**Deterministic detection, agentic diagnosis, human-gated action.** No model decides whether something is an anomaly. If it did, a false alarm would be unexplainable.

**Measured and asserted are visually distinct everywhere.** Facts computed from telemetry are one thing; the agent's reasoning is another, and the interface never blurs them. A reviewer should be able to trust the first and treat the second as a hypothesis.

**Read-only by default.** The diagnostic path holds no write credentials. Remediation is a proposal, not an action.

**Publish the failures.** The benchmark reports where the agent is wrong and why. A system that only reports its successes is not measurable.

## Results

Thirty faults injected into a synthetic estate and replayed blind. See [`docs/EVALUATION.md`](docs/EVALUATION.md) for the protocol.

| Fault class | Injected | Root cause found | Fix accepted |
|---|---|---|---|
| Source schema drift | 6 | — | — |
| Model bloat / inefficient DAX | 7 | — | — |
| Refresh window collision | 5 | — | — |
| Silent zero-row delivery | 4 | — | — |
| Concurrency & contention | 5 | — | — |
| Upstream gateway timeout | 3 | — | — |

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design, data flow and incident store schema.

```
Fabric REST · Capacity Metrics · semantic-link-labs
                    ↓
            Eventhouse (KQL)          ← telemetry
                    ↓
    KQL rules + PySpark variance engine   ← deterministic detection
                    ↓
      Agent loop (Agent Framework, notebook)   ← read-only investigation
                    ↓
        Lakehouse incident store (Delta)   ← evidence, diagnosis, proposal
                    ↓
         Human decision → pull request
```

## Repository layout

```
nightshift/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── EVALUATION.md
│   └── DECISIONS.md          # architecture decision records
├── estate/                   # the synthetic Fabric estate
│   ├── workspaces/           # item definitions, deployed via fabric-cli
│   ├── seed/                 # data generators
│   └── faults/               # fault injector — one module per fault class
├── collectors/
│   ├── rest_jobs.py          # job instances, item definitions
│   ├── capacity_metrics.py   # XMLA against the Capacity Metrics model
│   ├── activity_events.py    # admin activity + scanner APIs
│   └── model_stats.py        # semantic-link-labs: VertiPaq, BPA
├── detection/
│   ├── rules/                # KQL detection rules, one file each
│   └── variance/             # PySpark contribution decomposition
├── agent/
│   ├── orchestrator.py       # Agent Framework setup, routing
│   ├── agents/               # triage, diagnostician, remediation author
│   ├── tools/                # read-only tool wrappers, Pydantic contracts
│   └── fallback/             # deterministic templates for rate-limit degradation
├── store/
│   ├── schema.py             # incident store Delta schema
│   └── writer.py
├── eval/
│   ├── harness.py            # blind replay
│   ├── rubric.md             # scoring definitions
│   └── results/
├── report/                   # Power BI semantic model + report (TMDL, PBIP)
└── notebooks/                # Fabric notebook entry points
```

## Getting started

**Prerequisites**

- A Microsoft Entra work or school account. Personal accounts cannot access Fabric.
- A Fabric trial capacity. Set it to 64 CU in *Admin portal → Capacity settings → Trial* — the dropdown offers 4 or 64, and 4 will throttle a Spark session doing anything real.
- A model API key from a provider with a usable free tier.
- Python 3.11 locally for the collectors and the deployment scripts.

**Setup**

```bash
git clone <repo>
cd nightshift
pip install -r requirements.txt

# deploy the synthetic estate
python -m estate.deploy --tenant <tenant-id> --capacity <trial-capacity-id>

# seed data and start the fault schedule
python -m estate.seed --days 30
python -m estate.faults --schedule estate/faults/schedule.yaml
```

Then connect the `hl-nightshift` workspace to this repository via Fabric git integration and run `notebooks/00_bootstrap.ipynb` to create the Eventhouse, the incident store and the collector pipelines.

**Running the benchmark**

```bash
python -m eval.harness --faults 30 --blind --out eval/results/
```

The harness injects each fault, waits for detection, captures the agent's diagnosis with no knowledge of the injected cause, and scores it against the rubric in `eval/rubric.md`.

## Known limitations

These are documented rather than hidden. Each has a known cause.

- **Concurrency and contention are diagnosed poorly.** When two items compete for capacity, telemetry shows the victim clearly and the aggressor barely at all, so the agent ranks by consumption and names the wrong item. Consumption is not causation when items are queuing. Fixing it properly needs queue-wait telemetry the trial capacity does not expose.
- **No visibility beyond the gateway boundary.** Faults originating in an on-premises source are visible only as symptoms.
- **Model API key handling.** Without Key Vault access on the trial, the key is held as a workspace-restricted Lakehouse file. In production this belongs in Key Vault, fetched via managed identity. Documented as a deliberate deviation, not an oversight.
- **Workspace Monitoring is not used.** Its trial support is unconfirmed, so the collectors were built on the REST APIs and Capacity Metrics instead. On a paid capacity, Workspace Monitoring would replace roughly half of `collectors/`.
- **Estimated savings are estimates** until confirmed. The store records the estimate at proposal time and the measured figure 48 hours after merge, so the gap between the two is itself measurable.

## Out of scope, deliberately

Automatic application of any change. Real-time streaming detection — hourly batch is sufficient and far cheaper in CU. Capacity autoscaling, which requires ARM calls unavailable on trial. A bespoke web front end. Durable orchestration via Temporal, which matters in production and not here.

## Licence

MIT.
