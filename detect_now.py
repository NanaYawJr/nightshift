"""Run detection against the current estate. Scratch — not part of the system."""

from datetime import datetime

import pandas as pd

from common import storage
from detection.rules.silent_zero_row import WriteExpectation, detect

runs = storage.read("job_runs").drop_duplicates(subset=["run_id"])
commits = pd.read_csv("data/delta_commits.csv", parse_dates=["timestamp"])

# The Delta log timestamps are UTC but written without a marker.
commits["timestamp"] = commits["timestamp"].dt.tz_localize("UTC")

expectation = WriteExpectation(
    pipeline="pl_ingest_ledger_daily",
    table="ledger_daily",
    workspace="h1-finance-prod",
)

evaluate_before = datetime.fromisoformat("2026-10-04T18:03:40.731835+00:00")

incidents = detect(runs, commits, [expectation], evaluate_before)

eligible = runs[
    (runs.item_name == "pl_ingest_ledger_daily") & (runs.status == "Completed")
]
print(f"{len(eligible)} completed pipeline runs in telemetry")
print(f"{len(commits[commits.table_name == 'ledger_daily'])} commits to ledger_daily")
print(f"{len(incidents)} incidents\n")

for incident in incidents:
    print(incident)