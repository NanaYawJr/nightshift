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

---

## D-013 — Faults rewrite tables; schema drift is detected by diffing versions

**Date:** 2026-09-21
**Status:** Accepted

**Context.** The first run of a fault against real Delta, rather than against the
test suite's fake Spark. `SchemaDrift` was originally written to rename a column
in place:

```sql
ALTER TABLE Ledger RENAME COLUMN PostedTimestamp TO posted_ts
```

Checking the table first showed this would fail:

```
column mapping: none
reader version: 1
writer version: 2
```

In-place column renames require Delta column mapping. Enabling it is a one-way
protocol upgrade — the table cannot be returned to its previous protocol — and
some tools that read Delta tables do not handle column-mapped tables. The
semantic models this project is about to build on top of `Ledger` are among the
readers most likely to be affected.

**Decision.** Faults change a table by rewriting it, never by altering it in place.
`SchemaDrift` reads the table, renames the column in the frame, and overwrites the
table with `overwriteSchema` enabled. `ModelBloat` already worked this way. A shared
helper, `_overwrite`, now holds the pattern, and a test fails if any fault ever
issues an `ALTER` statement.

This is also more realistic. Real schema drift rarely arrives as a polite
`ALTER TABLE`; it arrives as an upstream source sending a full load in a new shape.

**Consequence for detection.** Verified on the real table. Inject and revert
produced three versions with identical operation names:

| Version | Operation | What actually happened |
|---|---|---|
| 0 | CREATE OR REPLACE TABLE AS SELECT | Initial seed |
| 1 | CREATE OR REPLACE TABLE AS SELECT | Column renamed |
| 2 | CREATE OR REPLACE TABLE AS SELECT | Column restored |

The transaction log does not distinguish a schema change from a plain reload. A
detection rule watching for a rename operation would see nothing.

So the `schema_drift` rule compares the table's schema at consecutive versions,
using Delta time travel to read each version's column list. This catches drift
however it is produced — by this injector, by a pipeline reload, or by a tool
that also avoids `ALTER TABLE`.

**Also consequent.** Rewriting costs more capacity than a metadata change, since
every row is rewritten. Acceptable under D-011: capacity is not the binding
constraint. The smoke test rewrote 2 million rows twice in 13 seconds.

**Revisit if.** A fault class genuinely cannot be expressed as a rewrite, or
rewrite cost becomes material as the estate grows.

---

## D-014 — The Spark runtime is pinned

**Date:** 2026-09-21
**Status:** Accepted

**Context.** A banner in the Fabric notebook announced that Runtime 2.0 — with a
new major Spark version and a new Delta version — becomes the default runtime in
late September 2026. Every notebook in the project currently runs on
"Workspace default", which means the Spark and Delta versions underneath them
would change without any action from the project.

The project's central claim rests on a reproducible benchmark: the same seed
must produce the same faults and the same measurements. An unannounced change of
Spark and Delta versions partway through would put every comparison across that
boundary in doubt — a change in results could come from the platform rather than
from the agent.

**Decision.** Create a Fabric Environment with an explicit runtime version and
attach it to every project notebook in place of the workspace default. The
platform changes only when the project decides it should.

**Consequences.** Improvements in the new runtime are not picked up automatically.
Runtime 2.0 may be tested deliberately later, in a separate Environment, and
adopted only if the benchmark reproduces on it.

The Environment is also the natural home for `semantic-link-labs`, which is
currently installed with `%pip install` at the start of every session. Moving it
into the Environment removes the per-session install and pins its version too —
two sources of drift closed by one change.

**Revisit if.** The pinned runtime approaches end of support before the project
is captured.

---

## D-015 — Model bloat is measured by refresh duration and Delta size, not memory

**Date:** 2026-09-28
**Status:** Accepted, supersedes an assumption in ARCHITECTURE.md

**Context.** `sm_revenue_core` was published as an Import model over the
Lakehouse SQL analytics endpoint, refreshed successfully, and confirmed to hold
2,000,000 rows via DAX. `vertipaq_analyzer` nevertheless reported a total model
size of 0 B with every table at 0 rows.

Reading the underlying DMV directly returned 280 segments with the expected
columns, but every one of them empty:

```
ISPAGEABLE  ISRESIDENT  VERTIPAQ_STATE
True        False       SKIPPED          240
False       True        SKIPPED           40

RECORDS_COUNT   0
ALLOCATED_SIZE  0
```

`VERTIPAQ_STATE = SKIPPED` throughout means the engine is not computing segment
statistics for this model, and 240 of 280 columns are pageable and not resident.
Querying the model to warm it made no difference. In-memory size is therefore
not a signal this project can rely on.

**Decision.** `model_bloat` is detected and quantified through three signals,
none of which depend on residency:

1. **Refresh duration**, from `collectors/refresh_history`. A near-unique text
   column on two million rows genuinely lengthens a refresh. This is INC-0412's
   headline symptom and survives unchanged.
2. **Delta table size and schema**, from `DESCRIBE DETAIL` and the transaction
   log. This is the cause rather than the symptom, and it is exact.
3. **Column cardinality**, computed from the table directly.

**Consequences.** Arguably a better design than the original. Memory size is a
single symptom measured at one point; this combination catches the cause
upstream in the Lakehouse and the symptom downstream in the model, and depends
on nothing the platform may decline to report.

ARCHITECTURE.md describes a `model_stats` tool returning VertiPaq column sizes.
That description is now wrong and must be corrected.

**Revisit if.** A model is published in a configuration where VertiPaq
statistics are computed, in which case memory size becomes an additional signal
rather than a replacement.

---

## D-016 — Collectors read DMVs directly rather than through semantic-link-labs

**Date:** 2026-09-28
**Status:** Accepted

**Context.** `vertipaq_analyzer` reported zeros where the underlying DMV,
queried directly with `fabric.evaluate_dax` against
`INFO.STORAGETABLECOLUMNSEGMENTS()`, returned 280 rows with the full column set:
`USED_SIZE`, `ALLOCATED_SIZE`, `RECORDS_COUNT`, `ISPAGEABLE`, `ISRESIDENT`,
`TEMPERATURE`, `LAST_ACCESSED`, `VERTIPAQ_STATE`.

The library was not surfacing information that was plainly available.

**Decision.** `collectors/model_stats` queries the DMVs directly. `sempy.fabric`
provides the connection; `semantic-link-labs` is retained for Best Practice
Analyzer, which has no straightforward DMV equivalent, but is not in the path
for size or cardinality statistics.

**Consequences.** Slightly more code in the collector, in exchange for removing
a library version from between the project and its primary measurement. Given
D-014 pinned that library specifically to prevent drift, removing the dependency
where possible is consistent rather than contradictory.

Also removes a failure mode that is hard to diagnose: a library returning zeros
looks identical to a model that is genuinely empty. A raw DMV returning rows
with zero values is at least legible.

**Revisit if.** A later version of `semantic-link-labs` handles this correctly
and offers something the raw DMV does not.

---

## D-017 — Revert is logical, not physical

**Date:** 2026-09-28
**Status:** Accepted

**Context.** Baseline capture of `ledger` after one inject-and-revert cycle of
`SchemaDrift`:

```
rows:     2,000,000
columns:  12
files:    8
size:     65,844,777 bytes (62.8 MB)
version:  2
```

The initial seed wrote **200 files**. After two rewrites the table holds the
same rows in **8 files** — Spark coalesced on write. The schema and data are
identical to the starting state; the physical layout is not.

**Decision.** `revert()` guarantees logical state only: same rows, same schema,
same values. It does not guarantee file count, file sizes or table version.
This is documented in the fault contract rather than fixed, because forcing an
identical physical layout would require rewriting with an explicit partition
count and would make faults slower and more brittle for no diagnostic benefit.

**Consequences for the benchmark.** File count and layout are not valid baseline
measurements across a benchmark run, because earlier faults change them. Any
detection rule keyed on `numFiles` would drift over a thirty-fault run and
produce findings caused by the harness rather than by the fault. Small-file
fragmentation is therefore explicitly excluded as a fault class, having already
been noted under D-012 as a candidate.

Valid baselines are row count, column set, and table size in bytes. All three
are unaffected by coalescing.

**Revisit if.** A fault class genuinely requires physical-layout fidelity, or if
file count becomes a detection signal worth having.

---

## D-018 — Workspace naming inconsistency, deferred

**Date:** 2026-09-28
**Status:** Known issue, deliberately deferred

**Context.** Four workspaces were created with the digit `1` rather than a
lowercase `l`: `h1-finance-prod`, `h1-ops-analytics`, `h1-sandbox`,
`h1-nightshift`. A fifth, `hl-care-quality`, uses the intended `l`. The two are
visually near-identical in most fonts.

This surfaced as a `WorkspaceNotFoundException` when code written from the
planning documents used `hl-finance-prod`.

**Decision.** Left as-is for now, at the cost of every reference in code and
documentation having to match the actual names rather than the intended ones.
Renaming is safe — item IDs are unaffected — but was deferred to avoid
interrupting the semantic model build.

**Consequences.** A real trap. A rule, a test fixture or a harness parameter
written from memory will fail in a way that is hard to see on screen. Until this
is resolved, workspace names belong in configuration rather than scattered
through code, so there is one place to correct when it is.

**Revisit:** before the evaluation harness is written. A benchmark run that
fails on a mistyped workspace name would be a poor use of week seven.

---

## D-019 — Import models pin their column list; model_bloat is widening, not addition

**Date:** 2026-09-30
**Status:** Accepted, supersedes the mechanism in the design mockup

**Context.** INC-0412 as designed said: an upstream change adds a
high-cardinality column, a semantic model importing the whole table picks it up
without anyone deciding to include it, and refresh time multiplies. The Power
Query source deliberately used no column selection so the import would behave
as a wildcard.

It does not. `TransactionNarrative` was added to the Lakehouse `ledger` table,
the SQL analytics endpoint was confirmed to expose it, and `sm_revenue_core` was
refreshed repeatedly. The model stayed at 35 columns throughout.

The TMDL explains why. Every column is stored explicitly:

```tmdl
column SourceRef
    dataType: string
    sourceColumn: SourceRef
```

The wildcard expands at publish time. Refresh re-reads the named columns and
nothing else. A new upstream column is never requested.

**Decision.** `ModelBloat` is replaced by `ColumnWidening`. Rather than adding a
column, the fault makes an existing one near-unique: `SourceRef` values gain the
row's `TransactionID` appended, taking the column from 864,973 distinct values
to 2,000,000. The schema does not change — same twelve columns, same names, same
types.

The fault class remains `model_bloat`, so the evaluation taxonomy is unaffected.
Only the mechanism changed.

**Verified end to end:**

| | Before | After |
|---|---|---|
| Columns | 12 | 12 |
| SourceRef distinct | 864,973 | 2,000,000 |
| Delta table size | 62.8 MB | 70.9 MB |
| Model size (VertiPaq) | 97.5 MB | 106.9 MB |

The model grew more than the table did — 9.4 MB against 8.1 — because
VertiPaq's dictionary was compressing harder than Parquet's, so losing it cost
more.

**Consequences.** A better fault than the original in three ways. It is
realistic: source systems change what they put in a field far more often than
they add fields. It is invisible to schema comparison, so the agent must reason
about cardinality against history rather than spot a new name. And it is exactly
invertible — `substring_index(SourceRef, '#', 1)` restores the original values —
where dropping an added column is only approximately a revert.

The injector refuses to run if the separator already occurs in the data, since
revert would otherwise truncate real values.

**Revisit if.** A model is ever published in a way that genuinely re-discovers
source columns at refresh, in which case column addition becomes viable again as
a second mechanism.

---

## D-020 — Direct Lake is the platform default and must be guarded against

**Date:** 2026-09-30
**Status:** Accepted

**Context.** Two full days were lost to measurements that made no sense: refreshes
completing in three seconds, `VERTIPAQ_STATE: SKIPPED` on every segment, a model
reporting 0 B while holding two million rows, and a new column that never
arrived however many times the model was refreshed.

The cause was that `sm_revenue_core` was a **Direct Lake** model, not an Import
one. Direct Lake pages columns from OneLake on demand rather than loading them,
so there is no import to lengthen, no resident footprint to measure, and
"refresh" is a metadata operation that takes seconds.

It was created accidentally, twice. Every obvious route in Fabric produces Direct
Lake: the Lakehouse's "New semantic model" button, the OneLake catalog connector
in Power BI Desktop, and opening an existing Direct Lake model in Desktop for
live editing. Only the SQL analytics endpoint via the plain SQL Server connector,
with Import explicitly chosen, produces an Import model.

**Decision.** Before any measurement is taken against a semantic model, its mode
is verified:

```python
fabric.evaluate_dax(
    dataset=..., workspace=...,
    dax_string='EVALUATE SELECTCOLUMNS(INFO.PARTITIONS(), "table", [Name], "mode", [Mode])'
)
```

Mode 5 is Direct Lake. Mode 0 or 1 on a model with `VERTIPAQ_STATE: COMPLETED`
segments is Import. `collectors/model_stats` records the mode alongside every
measurement, and detection rules that depend on refresh duration or model size
apply only to Import models.

**Consequences.** Direct Lake models cannot be diagnosed by the signals this
project currently uses. That is a limitation to state plainly rather than hide,
and it matters commercially: if Fabric's defaults push every new model to Direct
Lake, then Direct Lake is what real estates will contain.

Direct Lake has its own failure mode worth a future fault class — exceeding a
guardrail causes silent fallback to DirectQuery, which is slow, invisible, and
reports success. That is the same shape as every other fault here and would fit
the project well. Out of scope for the trial; recorded for later.

**Also noted.** The stranded Direct Lake model was renamed
`sm_revenue_core_direct_lake` rather than deleted, to avoid another wrong-item
mistake while both existed. It should be deleted once the Import model is stable.

---

## D-021 — Published models can be unrefreshable, and this is a fault class

**Date:** 2026-09-30
**Status:** Accepted

**Context.** The Import model published successfully and then refused every
refresh:

```
Premium_ASWL_Error: We cannot refresh this semantic model because this
semantic model uses a default data connection without explicit connection
credentials.
```

Publishing bound the SQL endpoint to "Default: Single Sign-On" rather than to an
explicit cloud connection. Fixing it required creating a named connection with
OAuth 2.0 credentials in Manage Connections and Gateways, then rebinding the
model to it and applying.

This is D-009 recurring. `MFI` has never successfully refreshed since April for
a closely related reason — `ModelRefreshDisabled_CredentialNotSpecified` — and
nobody noticed for five months.

Two models, both published by a competent person, both landing in a state where
they can never refresh, and neither surfacing that fact anywhere a person would
look.

**Decision.** `unrefreshable_model` is added to the fault taxonomy as a detection
target, though not yet as an injectable fault. Detection is straightforward from
data already collected: a model whose most recent refresh attempts all failed
with the same credential-related error code, or a model with a refresh schedule
and no successful run in its history.

**Consequences.** The evaluation taxonomy grows from six classes to seven, with
the seventh scored only against discovered instances until an injector exists.
`MFI` and the pre-fix state of `sm_revenue_core` are both real examples, found
in the author's own tenant rather than manufactured.

This strengthens the project's central claim rather than complicating it. A model
that reports success in the workspace list while being structurally incapable of
refreshing is exactly the silent failure the agent exists to find.

---

## D-022 — Faults must be revertible from sealed ground truth, not from a live object

**Date:** 2026-09-30
**Status:** Accepted, requires a change to `estate/faults/base.py`

**Context.** Three separate times, a Fabric notebook session timed out between
injecting a fault and reverting it. Each time `fault.revert(spark)` raised
`NameError: name 'fault' is not defined`, and the estate had to be restored by
hand-writing the inverse transform.

The fault framework holds revert state in `self._revert_state`, an attribute of a
live Python object. A notebook session lasts perhaps twenty minutes of idleness;
a thirty-fault benchmark run will last longer than that.

**Decision.** `Fault` gains a `from_ground_truth(truth)` classmethod that
reconstructs an injected instance from its sealed JSON alone. Every fault's
`parameters()` must therefore carry everything `_undo` needs — which they
already do, since the seal was designed to be a complete record of what was
done.

Any fault can then be reverted by any process at any time, given only the sealed
file:

```python
fault = Seal().load(fault_id)
ColumnWidening.from_ground_truth(fault).revert(spark)
```

**Consequences.** Removes a dependency on session lifetime from the benchmark,
which would otherwise have failed partway through a run and left the estate in an
unknown state — the worst possible outcome for a reproducible measurement.

It also makes cleanup possible after a crash. Currently a lost session means
manual restoration, and manual restoration is how an estate quietly drifts away
from its baseline without anyone recording it.

**Also implied.** Sealing must happen immediately on injection, before anything
else. A fault injected but not yet sealed is a fault that cannot be reverted.

**Revisit if.** Faults ever need state that cannot be serialised — which would be
a reason to question the fault, not the rule.