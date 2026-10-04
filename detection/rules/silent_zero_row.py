"""Detect pipeline runs that succeeded without delivering anything.

The first rule, and the one that defines the pattern the others follow: the
signal is an **absence** where something was expected, not a value that moved.

Verified behaviour this rule is built on (D-025):

- A copy activity reading a header-only file completes successfully, in its
  normal time, with no error anywhere.
- Delta writes no version at all when there is nothing to write, so the
  transaction log is silent too.
- `operationMetrics` is never populated by Data Factory (D-024), so
  `numOutputRows` is not available even when a commit does happen.

Each telemetry source is individually blind. The fault is only visible in the
correlation: a successful run with no corresponding commit.

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
    """

    pipeline: str
    table: str
    workspace: str


def detect(
    runs: pd.DataFrame,
    commits: pd.DataFrame,
    expectations: Iterable[WriteExpectation],
    evaluate_before: datetime,
    skew: timedelta = DEFAULT_SKEW,
) -> list[Incident]:
    """Find successful pipeline runs with no matching Delta commit.

    `runs` comes from collectors.rest_jobs: item_name, status, start_time,
    end_time, run_id, workspace_name.

    `commits` comes from collectors.delta_commits: table_name, version,
    timestamp, workspace_name.

    `evaluate_before` guards against the collection lag. The Delta collector
    runs on its own schedule, so the most recent pipeline runs will often have
    no commit recorded yet — not because none happened, but because nobody has
    looked. Evaluating those would produce a false positive on every latest run,
    every time. Callers pass the point up to which commit data is known
    complete, normally the last delta_commits collection time.
    """
    if runs.empty:
        return []

    incidents: list[Incident] = []

    for expectation in expectations:
        candidates = _eligible_runs(runs, expectation, evaluate_before)
        table_commits = _commits_for(commits, expectation)

        for run in candidates.itertuples():
            if _has_commit_in_window(table_commits, run.start_time, run.end_time, skew):
                continue

            incidents.append(_build_incident(run, expectation, table_commits))

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


def _has_commit_in_window(
    commits: pd.DataFrame, start: datetime, end: datetime, skew: timedelta
) -> bool:
    if commits.empty:
        return False

    within = (commits["timestamp"] >= start - skew) & (
        commits["timestamp"] <= end + skew
    )
    return bool(within.any())


def _build_incident(
    run, expectation: WriteExpectation, table_commits: pd.DataFrame
) -> Incident:
    last_commit = (
        table_commits["timestamp"].max() if not table_commits.empty else None
    )

    return Incident(
        rule=RULE,
        fault_class=FAULT_CLASS,
        workspace=expectation.workspace,
        item_name=expectation.pipeline,
        item_type="DataPipeline",
        severity="high",
        summary=(
            f"Run completed in {run.duration_seconds:.0f}s but wrote nothing to "
            f"{expectation.table}. Downstream data is unchanged and therefore stale."
        ),
        # The run id. One incident per run, however often detection re-runs.
        occurrence_key=run.run_id,
        measured={
            "run_id": run.run_id,
            "run_started": run.start_time.isoformat(),
            "run_ended": run.end_time.isoformat(),
            "run_duration_seconds": round(float(run.duration_seconds), 2),
            "run_status": run.status,
            "expected_table": expectation.table,
            "commits_in_window": 0,
            "last_commit_to_table": (
                last_commit.isoformat() if last_commit is not None else None
            ),
        },
    )