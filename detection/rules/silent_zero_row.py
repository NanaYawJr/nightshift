"""Detect pipeline runs that succeeded without delivering anything.

The first rule, and the one that defines the pattern the others follow: the
signal is an **absence** where something was expected, not a value that moved.

Verified behaviour this rule is built on:

- A copy activity reading a header-only file completes successfully, in its
  normal time, with no error anywhere. Twenty-three consecutive runs took 19 to
  23 seconds each, matching the healthy baseline exactly.
- Each of those runs still wrote a Delta commit. An empty copy commits.
- Every one of those commits added zero rows: the table held 355,000 rows
  before and after.
- `operationMetrics` is never populated by Data Factory (D-024), so the commit
  cannot be asked how much it carried; the count has to be derived from the
  data and is computed at collection time.

An earlier version of this rule checked only whether a commit existed in the
run's window. It reported a healthy estate for twenty-three hours while nothing
was delivered. The lesson is in D-026: a commit is proof that a write happened,
not proof that anything was written.

Expectations have to be declared. Telemetry cannot say that
`pl_ingest_ledger_daily` is supposed to write to `ledger_daily` — only that it
ran. That mapping is configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

from detection.incident import Incident

RULE = "silent_zero_row"
FAULT_CLASS = "silent_zero_row"

# Clock skew between the Fabric job API and the Delta log. A commit written at
# the very end of a run can carry a timestamp fractionally outside the run's
# window; without a margin those runs would be flagged incorrectly.
DEFAULT_SKEW = timedelta(seconds=90)


@dataclass(frozen=True)
class WriteExpectation:
    """A declaration that a pipeline writes to a table on every run.

    Required because no telemetry source records intent. A pipeline that ran and
    wrote nothing is indistinguishable from a pipeline that was never supposed
    to write anything, unless somebody says which it is.

    `minimum_rows` is how few rows counts as a failure. The default of 1 catches
    a delivery of nothing. Setting it higher catches a partial delivery — a feed
    that normally brings 5,000 rows and brings 12 is broken too, and looks just
    as healthy from the outside.
    """

    pipeline: str
    table: str
    workspace: str
    minimum_rows: int = 1


def detect(
    runs: pd.DataFrame,
    commits: pd.DataFrame,
    expectations: Iterable[WriteExpectation],
    evaluate_before: datetime,
    skew: timedelta = DEFAULT_SKEW,
) -> list[Incident]:
    """Find successful pipeline runs that delivered too few rows, or none.

    `runs` comes from collectors.rest_jobs: item_name, status, start_time,
    end_time, run_id, duration_seconds.

    `commits` comes from collectors.delta_commits: table_name, version,
    timestamp, rows_added.

    `evaluate_before` guards against the collection lag. The Delta collector
    runs on its own schedule, so the most recent pipeline runs will often have
    no commit recorded yet — not because none happened, but because nobody has
    looked. Evaluating those would produce a false positive on every latest run,
    every time. Callers pass the point up to which commit data is known
    complete, normally `delta_commits.complete_until()`.
    """
    if runs.empty:
        return []

    incidents: list[Incident] = []

    for expectation in expectations:
        candidates = _eligible_runs(runs, expectation, evaluate_before)
        table_commits = _commits_for(commits, expectation)

        for run in candidates.itertuples():
            delivered = _rows_delivered(table_commits, run.start_time, run.end_time, skew)

            # None means the commits in this window were not countable — a
            # vacuumed version, or outside the counting window. Unknown is not
            # the same as zero, and guessing produces false positives.
            if delivered is None:
                continue

            if delivered >= expectation.minimum_rows:
                continue

            incidents.append(_build_incident(run, expectation, delivered, table_commits))

    return incidents


def _eligible_runs(
    runs: pd.DataFrame, expectation: WriteExpectation, evaluate_before: datetime
) -> pd.DataFrame:
    """Successful, finished runs of this pipeline, old enough to judge.

    Failed runs are excluded deliberately. A loud failure is already visible to
    anyone looking at run history; this rule exists for the ones that are not.
    """
    eligible = runs[
        (runs["item_name"] == expectation.pipeline)
        & (runs["status"] == "Completed")
        & runs["start_time"].notna()
        & runs["end_time"].notna()
    ]

    return eligible[eligible["end_time"] < evaluate_before]


def _commits_for(commits: pd.DataFrame, expectation: WriteExpectation) -> pd.DataFrame:
    if commits.empty:
        return commits

    return commits[commits["table_name"] == expectation.table]


def _rows_delivered(
    commits: pd.DataFrame, start: datetime, end: datetime, skew: timedelta
) -> int | None:
    """Rows written to the table during this run.

    Zero when no commit landed in the window — nothing was written, which is
    the same outcome as a commit that carried nothing. None when a commit
    landed but its row count is unknown, which must not be read as zero.
    """
    if commits.empty:
        return 0

    in_window = commits[
        (commits["timestamp"] >= start - skew) & (commits["timestamp"] <= end + skew)
    ]

    if in_window.empty:
        return 0

    if in_window["rows_added"].isna().any():
        return None

    return int(in_window["rows_added"].sum())


def _build_incident(
    run, expectation: WriteExpectation, delivered: int, table_commits: pd.DataFrame
) -> Incident:
    last_commit = table_commits["timestamp"].max() if not table_commits.empty else None

    if delivered == 0:
        summary = (
            f"Run completed in {run.duration_seconds:.0f}s but wrote no rows to "
            f"{expectation.table}. Downstream data is unchanged and therefore stale."
        )
    else:
        summary = (
            f"Run completed in {run.duration_seconds:.0f}s and wrote only "
            f"{delivered:,} rows to {expectation.table}, below the expected "
            f"{expectation.minimum_rows:,}. Delivery is partial."
        )

    return Incident(
        rule=RULE,
        fault_class=FAULT_CLASS,
        workspace=expectation.workspace,
        item_name=expectation.pipeline,
        item_type="DataPipeline",
        severity="high",
        summary=summary,
        # The run id. One incident per run, however often detection re-runs.
        occurrence_key=run.run_id,
        measured={
            "run_id": run.run_id,
            "run_started": run.start_time.isoformat(),
            "run_ended": run.end_time.isoformat(),
            "run_duration_seconds": round(float(run.duration_seconds), 2),
            "run_status": run.status,
            "expected_table": expectation.table,
            "rows_delivered": delivered,
            "rows_expected_minimum": expectation.minimum_rows,
            "last_commit_to_table": (
                last_commit.isoformat() if last_commit is not None else None
            ),
        },
    )