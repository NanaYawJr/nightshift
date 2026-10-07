"""Tests for the silent_zero_row detection rule.

The rule is a pure function over two DataFrames, so these exercise the real
logic with no Spark, no Fabric and no network.

Most are false-positive tests. A detection rule that cries wolf is worse than
no rule, because people stop reading it — and this rule is especially exposed,
since it fires on an absence and absences have innocent causes.

The regression that shaped this file: the first version checked only whether a
commit existed in the run's window. An empty copy still commits, so the rule
reported a healthy estate for twenty-three hours while nothing was delivered.
Several tests below exist solely to stop that returning.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from detection.rules.silent_zero_row import WriteExpectation, detect

WS = "h1-finance-prod"
PIPELINE = "pl_ingest_ledger_daily"
TABLE = "ledger_daily"
BATCH = 5000

EXPECTATION = WriteExpectation(pipeline=PIPELINE, table=TABLE, workspace=WS)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
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


def commit_row(
    timestamp: datetime,
    rows_added: int | None = BATCH,
    table: str = TABLE,
    version: int = 1,
) -> dict:
    return {
        "workspace_name": WS,
        "table_name": table,
        "version": version,
        "timestamp": timestamp,
        "operation": "Update",
        "rows_added": rows_added,
        "metrics_rows": None,
    }


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --- The fault -------------------------------------------------------------


def test_commit_that_added_nothing_is_an_incident():
    """The regression. An empty copy commits; the commit carries no rows."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=15), rows_added=0)])

    incidents = detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)

    assert len(incidents) == 1
    assert incidents[0].measured["rows_delivered"] == 0
    assert incidents[0].severity == "high"


def test_no_commit_at_all_is_also_an_incident():
    """Nothing written is nothing written, whether or not a commit was made."""
    runs = frame([run_row("r1", NOW - timedelta(hours=1))])

    assert len(detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE)) == 1


def test_twenty_three_silent_runs_produce_twenty_three_incidents():
    """Matches the verified overnight behaviour exactly."""
    runs = frame(
        [run_row(f"r{n}", NOW - timedelta(hours=n + 1)) for n in range(23)]
    )
    commits = frame(
        [
            commit_row(
                NOW - timedelta(hours=n + 1) + timedelta(seconds=15),
                rows_added=0,
                version=n,
            )
            for n in range(23)
        ]
    )

    incidents = detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)

    assert len(incidents) == 23
    assert len({i.incident_id for i in incidents}) == 23


def test_incident_records_what_was_measured():
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start, duration_seconds=21.7)])
    commits = frame([commit_row(start + timedelta(seconds=10), rows_added=0)])

    measured = detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)[0].measured

    assert measured["run_id"] == "r1"
    assert measured["run_duration_seconds"] == 21.7
    assert measured["expected_table"] == TABLE
    assert measured["rows_delivered"] == 0
    assert measured["rows_expected_minimum"] == 1


# --- Partial delivery ------------------------------------------------------


def test_partial_delivery_is_an_incident_when_a_minimum_is_set():
    """A feed that normally brings 5,000 rows and brings 12 is broken too."""
    expectation = WriteExpectation(PIPELINE, TABLE, WS, minimum_rows=4000)
    start = NOW - timedelta(hours=1)

    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=10), rows_added=12)])

    incidents = detect(runs, commits, [expectation], EVALUATE_BEFORE)

    assert len(incidents) == 1
    assert "Delivery is partial" in incidents[0].summary


def test_partial_delivery_is_ignored_at_the_default_minimum():
    """Without a declared minimum, any rows at all count as delivery."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=10), rows_added=12)])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


# --- False positives: the dangerous direction ------------------------------


def test_a_normal_delivery_is_not_an_incident():
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=15))])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


def test_unknown_row_count_is_not_treated_as_zero():
    """A vacuumed version cannot be counted. Unknown is not empty, and
    guessing would flag healthy runs."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=15), rows_added=None)])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


def test_commit_just_outside_the_run_still_counts():
    """Clock skew between the job API and the Delta log is real."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start, duration_seconds=20)])
    commits = frame([commit_row(start + timedelta(seconds=80))])

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


def test_commit_far_outside_the_run_does_not_count():
    """The margin must be a margin, not a loophole."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(minutes=30))])

    assert len(detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)) == 1


def test_several_commits_in_one_window_are_summed():
    """A run that writes in two commits has delivered the total, not either."""
    expectation = WriteExpectation(PIPELINE, TABLE, WS, minimum_rows=4000)
    start = NOW - timedelta(hours=1)

    runs = frame([run_row("r1", start, duration_seconds=60)])
    commits = frame(
        [
            commit_row(start + timedelta(seconds=10), rows_added=2500, version=1),
            commit_row(start + timedelta(seconds=40), rows_added=2500, version=2),
        ]
    )

    assert detect(runs, commits, [expectation], EVALUATE_BEFORE) == []


def test_recent_runs_are_not_judged():
    """The guard against collection lag.

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
    runs = frame(
        [run_row("r1", NOW - timedelta(hours=1), item_name="pl_something_else")]
    )

    assert detect(runs, frame([]), [EXPECTATION], EVALUATE_BEFORE) == []


def test_commits_to_a_different_table_do_not_count():
    """A busy estate writes constantly. Only the expected table matters."""
    start = NOW - timedelta(hours=1)
    runs = frame([run_row("r1", start)])
    commits = frame([commit_row(start + timedelta(seconds=10), table="repayments")])

    assert len(detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE)) == 1


def test_a_healthy_day_produces_nothing():
    """Twenty-four hourly runs, each delivering its batch. Silence is correct."""
    runs = frame(
        [run_row(f"r{n}", NOW - timedelta(hours=n + 1)) for n in range(24)]
    )
    commits = frame(
        [
            commit_row(
                NOW - timedelta(hours=n + 1) + timedelta(seconds=15), version=n
            )
            for n in range(24)
        ]
    )

    assert detect(runs, commits, [EXPECTATION], EVALUATE_BEFORE) == []


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