"""Run detection against the current estate.

Scratch. Not part of the system — it exists because detection's two inputs live
in different places: pipeline runs come from the local collectors, Delta
commits come from a notebook. Replaced once detection runs where the data is.

Usage:
    python -m collectors.rest_jobs     # refresh the run history first
    python detect_now.py
"""

import collections
from datetime import datetime

import pandas as pd

from common import storage
from detection.rules.silent_zero_row import WriteExpectation, detect

# --- Inputs ----------------------------------------------------------------

# Pipeline runs: live, from the Fabric REST API via collectors.rest_jobs.
runs = storage.read("job_runs").drop_duplicates(subset=["run_id"])

# Delta commits: pasted across from the notebook's delta_commits collection.
commits = pd.read_csv("data/delta_commits.csv", parse_dates=["timestamp"])

# The Delta log writes UTC without a marker. Comparing naive against aware
# timestamps raises TypeError — the same defect as D-007, third appearance.
commits["timestamp"] = commits["timestamp"].dt.tz_localize("UTC")

# What the pipeline is supposed to do. No telemetry source records intent.
expectation = WriteExpectation(
    pipeline="pl_ingest_ledger_daily",
    table="ledger_daily",
    workspace="h1-finance-prod",
)

# Up to when the commit data is known complete. Printed by
# delta_commits.complete_until() at collection time. Runs finishing after this
# are not judged — their commits may simply not have been collected yet.
EVALUATE_BEFORE = "2026-10-07T14:28:33.298820+00:00"

# --- Detection -------------------------------------------------------------

incidents = detect(
    runs,
    commits,
    [expectation],
    evaluate_before=datetime.fromisoformat(EVALUATE_BEFORE),
)

# --- Output ----------------------------------------------------------------

eligible = runs[
    (runs.item_name == expectation.pipeline) & (runs.status == "Completed")
]
daily_commits = commits[commits.table_name == expectation.table]

print(f"{len(eligible)} completed runs of {expectation.pipeline}")
print(f"{len(daily_commits)} commits to {expectation.table}")
print(f"{len(incidents)} incidents\n")

if incidents:
    # Grouped by day. A flat list of 68 identical lines says less than the
    # shape of when they happened — the gaps are where the estate was healthy.
    print("incidents by day")
    by_day = collections.Counter(i.measured["run_started"][:10] for i in incidents)
    for day, count in sorted(by_day.items()):
        print(f"  {day}  {count:>3}")

    print("\nfirst and last")
    ordered = sorted(incidents, key=lambda i: i.measured["run_started"])
    print(f"  {ordered[0].measured['run_started']}  {ordered[0].incident_id}")
    print(f"  {ordered[-1].measured['run_started']}  {ordered[-1].incident_id}")

    print("\nsample")
    print(f"  {ordered[0]}")

    NameError

oct2 = runs[
    (runs.item_name == expectation.pipeline)
    & (runs.start_time >= "2026-10-02 11:00")
    & (runs.start_time <= "2026-10-02 17:00")
]
print("\n2 Oct runs")
print(oct2[["run_id", "status", "start_time", "duration_seconds"]].sort_values("start_time").to_string(index=False))

print("\n2 Oct commits")
c = commits[(commits.table_name == "ledger_daily") & (commits.version.between(19, 26))]
print(c[["version", "timestamp", "rows_added"]].sort_values("version").to_string(index=False))