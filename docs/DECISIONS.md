# Decision records

Append-only. Each entry records a decision at the moment it was made, not in hindsight. Superseded decisions stay in place, marked as such, with the entry that replaced them named.

---

## D-001 — Detection thresholds are percentage-based, not absolute

**Date:** 2026-09-11
**Status:** Accepted

**Context.** Before writing any detection rule, VertiPaq Analyzer and Best Practice Analyzer were run against an existing semantic model (`MFI`) to calibrate against real data rather than assumption. The model turned out to be 930 KB across 10 tables, the largest holding 350 rows. Any absolute threshold — "flag columns over 500 MB" — returns nothing on a model this size and everything on a production estate.

**Decision.** All size and cost thresholds are expressed as a share of their parent: percentage of table, percentage of model, percentage of capacity consumed in a window. Absolute thresholds are used only where the unit is inherently absolute, such as refresh duration in seconds or rows delivered by a pipeline run.

**Consequences.** Rules port between estates of any size without retuning, which matters because the eventual clients have estates nothing like the development one. The cost is losing the ability to say "anything over X GB is always wrong", which is occasionally a true and useful statement.

**Revisit if.** A real estate produces findings that are large in percentage terms but trivially small in absolute cost, generating noise.

---

## D-002 — Only the Performance category of BPA findings feeds detection

**Date:** 2026-09-11
**Status:** Accepted

**Context.** The BPA run against `MFI` returned 292 findings: 13 errors and 7 warnings under DAX Expressions, 83 warnings and 33 info under Formatting, 99 info and 2 warnings under Maintenance, 44 warnings and 1 info under Performance.

Formatting and Maintenance together account for 216 of the 292. They cover naming conventions, descriptions and documentation hygiene — real concerns for a model author, irrelevant to reliability or capacity cost.

Separately, all 13 DAX Expression errors were unqualified column references inside `DateTableTemplate_16fe5cda-...`, an auto-generated hidden date table. It cannot be edited, was not authored by anyone, and appears in every model with auto date/time enabled.

**Decision.** Detection consumes BPA findings where `Category == "Performance"` and severity is warning or higher, excluding any object belonging to an auto-generated date table (`DateTableTemplate_*`, `LocalDateTable_*`). This reduced 292 findings to roughly 40 on the calibration model.

**Consequences.** Nightshift stays focused on cost and reliability and does not become a linter. A genuine performance problem that BPA happens to file under Maintenance would be missed; none were observed in calibration, but this is the known blind spot.

**Revisit if.** A missed incident traces back to a finding in an excluded category.

---

## D-003 — The synthetic estate needs a fact table in the low millions of rows

**Date:** 2026-09-11
**Status:** Accepted

**Context.** The original estate plan did not specify data volume. Calibration showed why that was a gap: on a 930 KB model, the largest single finding is worth a fraction of a capacity unit. Detection logic would run, and every estimated saving would be indistinguishable from noise.

The project's core claim is that it converts an unexplainable capacity bill into a specific, quantified, fixable cause. That claim cannot be demonstrated on a dataset where nothing costs anything.

**Decision.** The synthetic estate's primary fact table is generated at low-millions row scale, with realistic cardinality distribution — a small number of very high-cardinality columns and a long tail of low-cardinality ones, rather than the uniform distribution naive generators produce.

**Consequences.** Seed generation takes longer to write and longer to run, and consumes more of the trial's 1 TB OneLake allowance and its capacity units. In exchange, CU savings become measurable and the variance engine is exercised on the column-size distribution it will actually meet in production.

**Revisit if.** Capacity monitoring shows the estate itself consuming enough CU to threaten the trial. Volume is the first thing to cut.

---

## D-004 — MFI is a discovered item, not a fault population

**Date:** 2026-09-11
**Status:** Accepted

**Context.** `MFI` is a pre-existing semantic model in the tenant, last refreshed 24 April 2026, holding synthetic microfinance data mirrored from real structure. It has a genuine star schema — `Loan_Portfolio`, `Clients`, `Monthly_Repayments`, `Branch_Summary` — and nine relationships.

It was initially considered as a source of real faults, on the reasoning that faults found in the wild are more convincing than faults injected. Calibration found three genuine findings, but also established the model is far too small to produce meaningful cost signal.

The three findings:

1. **Auto date/time is enabled alongside a purpose-built `DATE` table.** `LocalDateTable_f8cca73a-...` accounts for 6.52% of the model and `DateTableTemplate_16fe5cda-...` for 3.83% — 10.4% of the database duplicating work `DATE` already does. Note the complication: `Branch_Summary[MonthStart]` has a relationship to the auto-generated `LocalDateTable`, not to `DATE`. Disabling auto date/time without repointing that relationship breaks the model. This is a useful test case for fix quality rather than diagnostic accuracy.
2. **Unique string identifiers dominate their tables.** `Payment_ID` and `Receipt_Number` are 21.83% and 21.76% of `Monthly_Repayments`, each with cardinality equal to row count, so no compression is possible. Dictionary size dwarfs data size in both. `Phone_Number` is 17.73% of `Clients` with the same shape.
3. **The model is cold.** Every column reports `Temperature 0.0` and `Last Accessed NaT`. Combined with the April refresh date, this is the `abandoned_item` pattern.

**Decision.** `MFI` is included in the estate as a `discovered` item rather than a seeded one. Incidents originating from it are marked `origin: discovered` in the incident store and are excluded from benchmark scoring, because there is no sealed ground truth for them. It is used for one purpose: demonstrating that the agent finds real problems in a workspace nobody was maintaining.

**Consequences.** The incident store needs an `origin` field distinguishing `injected` from `discovered`, and the evaluation harness must ignore discovered incidents when computing accuracy. Mixing them would inflate or deflate the figures depending on luck.

**Revisit if.** A second real model of meaningful size becomes available, in which case discovered incidents might warrant their own scoring approach.

---

## D-005 — Workspace Monitoring is not used as a telemetry source

**Date:** 2026-09-11
**Status:** Accepted

**Context.** Workspace Monitoring is the natural backbone for this project — it lands refresh, query, Spark and Eventhouse logs into a KQL database directly. Its support on trial capacity could not be confirmed from documentation.

**Decision.** Collection is built on the Fabric REST APIs, the Capacity Metrics semantic model over XMLA, Delta transaction logs, and `semantic-link-labs`. All are confirmed available on trial.

**Consequences.** More collector code than strictly necessary, and some telemetry is coarser than Workspace Monitoring would provide — notably query-level detail. In exchange, no load-bearing dependency that might fail on day one. On paid capacity, Workspace Monitoring would replace roughly half of `collectors/`, and that migration is the first post-trial task.

**Revisit if.** Workspace Monitoring is confirmed working on trial capacity, or the project moves to an F2.

---

## D-006 — XMLA endpoint left at Read Only

**Date:** 2026-09-11
**Status:** Accepted

**Context.** Trial capacities do not expose the Power BI workload settings blade where the XMLA endpoint mode is configured. The tenant-level XMLA setting is enabled by default, but the per-capacity Read/Write toggle is unavailable.

**Decision.** Proceed on Read Only. Collectors need read access to VertiPaq statistics and the Capacity Metrics model; neither requires write.

**Consequences.** The remediation path cannot write TMDL back to a semantic model directly. This is consistent with the architecture regardless — remediation is delivered as a pull request against the workspace repository, not as a direct model edit. So the restriction costs nothing in practice, which is worth noting: it was discovered as a limitation and turned out to align with an existing design decision.

**Revisit if.** The project moves to a paid capacity, or a remediation type emerges that genuinely requires direct model write.

---

## D-007 — Duration rules exclude interactive runs

**Date:** 2026-09-13
**Status:** Accepted

**Context.** The first live collection returned one job run: the calibration
notebook, `job_type: RunNotebookInteractive`, `invoke_type: Manual`, with a
duration of 2,406 seconds. The notebook's cells executed in roughly 50 seconds.
The remaining 40 minutes is Spark session lifetime — the session stays alive
until it idles out, and the job record measures the session, not the work.

A naive duration anomaly rule would treat every interactive notebook run as a
40-minute job and fire constantly on human activity.

**Decision.** Duration-based detection rules filter to `invoke_type ==
"Scheduled"`. Manual and interactive runs are collected and stored but excluded
from anomaly detection. Where a notebook's execution time is genuinely needed,
it must come from cell-level telemetry rather than the job record.

**Consequences.** Someone manually running a pathological notebook and burning
capacity will not be flagged by duration rules. Capacity-based rules still catch
it, which is the correct division — duration is a reliability signal, consumption
is a cost signal, and they should not be conflated.

**Revisit if.** Cell-level notebook telemetry becomes available, or manual runs
turn out to be a meaningful share of capacity consumption in the estate.

---

## D-004a — Addendum: the discovered population is larger than MFI

**Date:** 2026-09-13
**Status:** Accepted, extends D-004

**Context.** The first inventory collection returned six items in
`PowerBiProjects`, not the four assumed during calibration. Alongside `MFI`
there is a `Road Accident` semantic model, report and dashboard, none of which
featured in any planning and none of which had been opened in months. The
dashboard is named `Road Accident.pbix`, the default title Power BI assigns when
a dashboard is auto-created on publish from Desktop — it was almost certainly
not made deliberately.

**Decision.** The `Road Accident` items join `MFI` as `origin: discovered`. The
`abandoned_item` fault class now has real candidates rather than only injected
ones, and its detection rule will be written against these before any synthetic
equivalent is built.

**Consequences.** Strengthens the claim the project is built on: the agent found
something in a workspace its own author had forgotten existed. That is a better
demonstration than any injected fault. Discovered incidents remain excluded from
benchmark scoring per D-004, since there is no sealed ground truth for them.

---

## D-008 — The diagnostic principal holds Member, not Viewer

**Date:** 2026-09-13
**Status:** Accepted, with reservations

**Context.** The architecture states the diagnostic service principal is
read-only, and it was granted Viewer on `PowerBiProjects` accordingly. Reading
semantic model inventory worked; reading refresh history returned 403.

```
GET /v1.0/myorg/groups/{groupId}/datasets/{datasetId}/refreshes
403 Client Error: Forbidden
```

Raising the principal to Member resolved it immediately. This is a quirk of the
Power BI permission model rather than anything wrong with the request: refresh
history is treated as a dataset-management operation rather than a read, so it
requires a write-capable workspace role.

Member is not read-only. It can publish, modify and delete items in the
workspace.

**Decision.** The diagnostic principal is granted Member where refresh history
is required. The read-only intent is preserved in code rather than in
permissions: `common/powerbi_api.py` and `common/fabric_api.py` expose only
`get`-shaped functions, and no module in `collectors/` issues anything but GET.

**Consequences.** Isolation is weaker than the architecture claims, and the
architecture document must be corrected rather than left aspirational. The
mitigation is real but is a code-level guarantee, not a platform-level one — a
mistake in `collectors/` could now do damage that Viewer would have prevented.

Two alternatives were considered and rejected for now. Dropping refresh history
from the REST collectors and taking it from `semantic-link-labs` in a notebook
would keep the principal at Viewer, but moves collection into the Fabric runtime
and away from the local, testable, capacity-free development loop. Using a
second principal scoped only to refresh history adds a credential to manage for
one endpoint.

**Revisit if.** Fabric item-level permissions come to cover the refresh history
endpoint, or collection moves into the notebook runtime for other reasons. This
is the single weakest point in the current security posture and should not be
carried into a client deployment unexamined.

---

## D-009 — MFI has never successfully refreshed

**Date:** 2026-09-13
**Status:** Accepted, corrects D-004

**Context.** With refresh history readable, `MFI` returned exactly three runs,
all on 24 April 2026 within a five-minute window, all `OnDemand`, all `Failed`,
all with the same error:

```
{"errorCode":"ModelRefreshDisabled_CredentialNotSpecified"}
```

No runs before, none since. The data source credentials were never configured
after the model was published, three manual attempts failed in under a second
each, and nobody returned to it.

This corrects an assumption carried since calibration. The 24 April date was
read as "last refreshed"; it is in fact the last *attempted* refresh. `MFI` has
never successfully refreshed in the service. Its contents are whatever was in
the PBIX at upload time, five months ago.

**Decision.** `MFI` is reclassified within the discovered population. It is not
a stale model that has drifted out of date — it is a model that never worked and
was never noticed, which is a distinct and more interesting fault.

The `stale_model` detection rule is split accordingly:

- **Overdue** — last successful refresh older than the model's own schedule
  interval by some margin.
- **Never succeeded** — no run with status `Completed` in the available history.
- **Failing repeatedly** — the most recent runs share an identical error code,
  indicating a configuration fault rather than a transient one.

The third is the most valuable. A repeated identical error code is a strong
signal that nobody is watching, because a human who saw it would either fix it
or turn the schedule off.

**Consequences.** This is the project's thesis demonstrated in the author's own
tenant, found by the author's own collector, before any synthetic fault existed.
It goes in the write-up. It also means `MFI` cannot be used as a baseline for
refresh duration anomaly detection, since it has no successful run to baseline
against — that baseline must come from the synthetic estate.

**Also noted.** The API returns a `refreshAttempts` array, empty for these runs
but populated where a refresh was retried internally. Not currently captured by
`collectors/refresh_history.py`. Worth adding before the detection rules are
written, since retry counts distinguish a transient failure from a hard one.

**Revisit if.** Credentials are configured and `MFI` begins refreshing, at which
point it becomes an ordinary model and loses its value as a discovered fault.
Recommendation: leave it broken until the project is captured.

---

## D-010 — Spark session lifetime dominates capacity cost

**Date:** 2026-09-16
**Status:** Accepted

**Context.** First reading from the Capacity Metrics app after seeding the
estate. Four items have consumed capacity since the trial began:

| Workspace | Item kind | Item | CU (s) | Duration (s) |
|---|---|---|---|---|
| hl-finance-prod | SynapseNotebook | 01_seed_estate | 22,486 | 2,557 |
| PowerBiProjects | SynapseNotebook | 00_calibration_mfi | 20,254 | 2,402 |
| hl-finance-prod | Lakehouse | lh_finance_bronze | 321 | 0.16 |
| PowerBiProjects | Dataset | MFI | 28 | 1.93 |

`01_seed_estate` generated 2.7 million rows across five tables and wrote them
as Delta. `00_calibration_mfi` ran two `semantic-link-labs` calls against a
930 KB model — roughly 50 seconds of actual work.

They cost within 10% of each other.

This is D-007 quantified. The job record bills Spark session lifetime, not
execution time. A notebook that does almost nothing but stays attached costs
roughly what a notebook doing serious work costs. Two nearly-idle sessions
account for over 99% of capacity consumed to date.

**Decision.** Collectors that require the Spark runtime batch all their work
into a single session per run. `collectors/model_stats` will iterate every
semantic model inside one notebook invocation rather than being invoked per
model. Session-bound collection runs daily, not hourly — the marginal cost of
more frequent collection is session startup, not the work itself.

Collectors that do not need Spark stay in local Python against the REST APIs,
where they cost nothing at all. This is now a cost argument as well as a
testability one.

**Consequences.** Model statistics are up to 24 hours stale. Acceptable:
VertiPaq column sizes and BPA findings change when a model is edited or its
schema drifts, which is not an hourly event. Refresh history, which *is* time
sensitive, comes from the REST API and stays hourly.

Design constraint for the agent layer: the diagnostician's `model_stats` and
`run_bpa` tools cannot each open their own session. They read from the daily
collection, or they share one session, or the agent becomes the most expensive
item in the estate.

**Revisit if.** Fabric introduces a lighter runtime for semantic-link-labs
style work, or session startup cost falls materially.

---

## D-011 — Capacity is not the binding constraint

**Date:** 2026-09-16
**Status:** Accepted, relaxes an assumption in ARCHITECTURE.md

**Context.** After seeding the full estate, average capacity utilisation over
24 hours is 0.05%, peak 0.41%, with zero throttling and zero rejected
operations on an FTL64.

The architecture document budgets Nightshift at under 8 CU-h/day and imposes a
no-sub-hourly-schedules rule, both written before any measurement existed. The
total consumption of the project to date is roughly 12 CU-h across six days.

**Decision.** The no-sub-hourly rule is relaxed from a hard constraint to a
default. Where a detection rule genuinely benefits from tighter granularity it
may run more frequently, subject to D-010 — the constraint that actually binds
is Spark session count, not capacity units.

The fault injector may rewrite tables freely rather than being rationed.

**Consequences.** Removes a self-imposed limitation that was costing design
flexibility for no measured benefit. The CU budget table in ARCHITECTURE.md is
now known to be conservative by roughly an order of magnitude and should be
restated against real figures rather than left as an estimate.

Caution retained: the trial cannot be paused or reset, so the *absence* of a
binding constraint today is not licence to stop watching. A runaway agent loop
in week 6 could still consume meaningfully, and iteration caps stay.

**Revisit if.** The fault injector or the agent loop moves utilisation above
roughly 20% sustained.

---

## D-012 — PostedTimestamp is retained as an unplanned bloat candidate

**Date:** 2026-09-16
**Status:** Accepted

**Context.** Cardinality check against the seeded Ledger at full scale:

```
TransactionID      2,000,000   100.00% of rows
PostedTimestamp    1,872,666    93.63%
SourceRef            864,973    43.25%
Amount               343,485    17.17%
ClientID              45,000     2.25%
TransactionDate          562     0.03%
PostedBy                  30
Branch                    12
ProductCode                8
Channel                    5
Status                     4
CurrencyCode               3
```

Five and a half orders of magnitude between widest and narrowest, which is the
distribution D-003 called for.

`PostedTimestamp` at 93.63% was not designed. Second-granularity timestamps are
near-unique by construction, and in VertiPaq will be among the most expensive
columns in any model built on this table.

**Decision.** It stays. It is a genuine and common real-world bloat cause that
does not *look* like one — a timestamp reads as innocuous where a free-text
narrative field reads as suspicious. It becomes a second `model_bloat`
candidate alongside the injected `TransactionNarrative`, and arguably the more
interesting of the two, since diagnosing it requires the agent to reason about
cardinality rather than pattern-match on "text column".

**Consequences.** The `model_bloat` fault class now has a discovered instance
as well as injected ones. Like `MFI` under D-004, discovered instances are
excluded from benchmark scoring for want of sealed ground truth.

**Also confirmed.** The Delta transaction log carries `numOutputRows` in
`operationMetrics` — 2,000,000 on the Ledger's initial commit. This was an
assumption until now. The `silent_zero_row` fault class is viable: a pipeline
that reports success while writing nothing produces a commit with
`numOutputRows: 0`, readable via `DESCRIBE HISTORY` without any additional
telemetry source.

**Also noted.** The initial write produced `numFiles: 200`. Small-file
fragmentation is a real performance pattern and a plausible future finding, but
is not currently a fault class.