"""Tests for the silent_zero_row detection rule.

The rule is a pure function over two DataFrames, so these exercise the real
logic with no Spark, no Fabric and no network.

Most of these are false-positive tests. A detection rule that cries wolf is
worse than no rule, because people stop reading it — and this rule is
especially exposed, since it fires on an absence and absences have many
innocent causes.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from detection.rules.silent_zero_row import (
    DEFAULT_SKEW,
    WriteExpectation,
    detect,
)

WS = "h1-finance-prod"
PIPELINE = "pl_ingest_ledger_daily"
TABLE = "ledger_daily"

EXPECTATION = WriteExpectation(pipeline=PIPELINE, table=TABLE, workspace=WS)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
EVALUATE_BEFORE = NOW


def run_row(
    run_id: str,
    start: datetime,
    duration_seconds: float = 22.0,
    status: str = "Completed",
    item_name: str = PIPELINE,
) -> dict:
    return {
        "workspace_name": WS,
        "item_name": item_name,
        "item_type": "DataPipeline",
        "run_id": run_id,
        "status": status,
        "start_time": start,
        "end_time": start + timedelta(seconds=duration_seconds),
        "duration_seconds": duration_seconds,
    }


def commit_row(timestamp: datetime, table: str = TABLE, version: int = 1) -> dict:
    return {
        "workspace_name": WS,
        "table_name": table,
        "version": version,
        "timestamp": timestamp,
        "operation": "Update",
        "num_output_rows": None,
    }


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- The fault -------------------------------------------------------------


def test_successful_run_with_no_commit_is_an_incident():
    """The fault itself. Pipeline green, nothing written."""
    runs = frame([run_row("r1", NOW - timedelta(hours=1))])
    commits = frame([commit_row(NOW - timedelta(hours=3))])

    incidents = detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)

    assert len(incidents) == 1
    assert incidents[0].fault_class == "silent_zero_row"
    assert incidents[0].severity == "high"
    assert incidents[0].item_name == PIPELINE


def test_incident_records_what_was_measured():
    runs = frame([run_row("r1", NOW - timedelta(hours=1), duration_seconds=21.7)])
    commits = frame([])

    incident = detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)[0]

    assert incident.measured["run_id"] == "r1"
    assert incident.measured["run_duration_seconds"] == 21.7
    assert incident.measured["expected_table"] == TABLE
    assert incident.measured["commits_in_window"] == 0


def test_three_consecutive_silent_runs_produce_three_incidents():
    """Matches the verified behaviour: three runs, none wrote anything."""
    runs = frame(
        [
            run_row("r1", NOW - timedelta(hours=3)),
            run_row("r2", NOW - timedelta(hours=2)),
            run_row("r3", NOW - timedelta(hours=1)),
        ]
    )

    incidents = detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE)

    assert len(incidents) == 3
    assert len({i.incident_id for i in incidents}) == 3


# --- False positives: the dangerous direction ------------------------------


def test_run_with_a_matching_commit_is_not_an_incident():
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=15))])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


def test_commit_just_outside_the_run_still_counts():
    """Clock skew between the job API and the Delta log is real."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start, duration_seconds=20)])
    commits = frame([commit_row(start + timedelta(seconds=20) + timedelta(seconds=60))])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


def test_commit_far_outside_the_run_does_not_count():
    """The margin must be a margin, not a loophole."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(minutes=30))])

    assert len(detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)) == 1


def test_recent_runs_are_not_judged():
    """The guard against the collection lag.

    Commits are gathered on their own schedule, so the newest runs often have
    no commit recorded yet — because nobody has looked, not because none
    happened. Without this, every latest run would be flagged, every time.
    """
    runs = frame([run_row("r1", NOW + timedelta(minutes=5))])

    assert detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_failed_runs_are_ignored():
    """A loud failure is already visible. This rule is for the quiet ones."""
    runs = frame([run_row("r1", NOW - timedelta(hours=1), status="Failed")])

    assert detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_in_progress_runs_are_ignored():
    rows = [run_row("r1", NOW - timedelta(hours=1), status="InProgress")]
    rows[0]["end_time"] = None
    rows[0]["duration_seconds"] = None

    assert detect(frame(rows), frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_other_pipelines_are_not_judged_by_this_expectation():
    runs = frame([run_row("r1", NOW - timedelta(hours=1), item_name="pl_something_else")])

    assert detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_commits_to_a_different_table_do_not_count():
    """A busy estate writes constantly. Only the expected table matters."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=10), table="repayments")])

    assert len(detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)) == 1


# --- Determinism -----------------------------------------------------------


def test_the_same_run_yields_the_same_incident_id():
    """Detection runs hourly. The same problem must not be filed repeatedly."""
    runs = frame([run_row("r1", NOW - timedelta(hours=1))])

    first = detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE)[0]
    second = detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE)[0]

    assert first.incident_id == second.incident_id


def test_different_runs_yield_different_incident_ids():
    runs = frame(
        [
            run_row("r1", NOW - timedelta(hours=2)),
            run_row("r2", NOW - timedelta(hours=1)),
        ]
    )

    incidents = detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE)

    assert incidents[0].incident_id != incidents[1].incident_id


# --- Degenerate inputs -----------------------------------------------------


def test_no_runs_means_no_incidents():
    assert detect(frame([]), frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_no_expectations_means_no_incidents():
    """A pipeline nobody declared an expectation for is not this rule's business."""
    runs = frame([run_row("r1", NOW - timedelta(hours=1))])

    assert detect(runs, frame([]), [], EVALUATE_BEFORE) == []


def test_multiple_expectations_are_evaluated_independently():
    other = WriteExpectation(pipeline="pl_ops_hourly", table="ops_events", workspace=WS)

    runs = frame(
        [
            run_row("r1", NOW - timedelta(hours=1)),
            run_row("r2", NOW - timedelta(hours=1), item_name="pl_ops_hourly"),
        ]
    )

    incidents = detect(runs, frame([]), [EXPECTATION, other], EVALUATE_BEFORE)

    assert {i.item_name for i in incidents} == {PIPELINE, "pl_ops_hourly"}