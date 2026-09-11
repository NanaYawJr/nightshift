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
