"""Deduplication tests.

The important ones are not "does it remove duplicates" but "does it keep the
right row" and "does it refuse to guess". A dedup that silently drops a real
row is worse than no dedup at all, because a missing row cannot be seen in a
count.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from common import storage
from detection import telemetry

EARLIER = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
LATER = EARLIER + timedelta(hours=1)


@pytest.fixture(autouse=True)
def local_backend(tmp_path):
    storage.use_local(tmp_path)
    yield
    storage.use_local()


# --- Choosing between duplicates ------------------------------------------


def test_duplicate_rows_collapse_to_one():
    """The real case: Cell 4 ran twice and wrote 168 runs twice."""
    storage.write("job_runs", [{"run_id": "r1", "status": "Completed"}])
    storage.write("job_runs", [{"run_id": "r1", "status": "Completed"}])

    assert len(storage.read("job_runs")) == 2
    assert len(telemetry.load("job_runs")) == 1


def test_the_latest_observation_wins():
    """A run collected mid-flight and collected again once finished must
    resolve to finished. First-wins would freeze it as InProgress forever."""
    frame = pd.DataFrame(
        [
            {"run_id": "r1", "status": "InProgress", "collected_at": EARLIER},
            {"run_id": "r1", "status": "Completed", "collected_at": LATER},
        ]
    )

    result = telemetry.deduplicate(frame, ("run_id",))

    assert len(result) == 1
    assert result.iloc[0]["status"] == "Completed"


def test_order_in_the_frame_does_not_decide():
    """Same two observations, newest first in the frame. collected_at decides,
    not position, or the result would depend on how Delta returned the rows."""
    frame = pd.DataFrame(
        [
            {"run_id": "r1", "status": "Completed", "collected_at": LATER},
            {"run_id": "r1", "status": "InProgress", "collected_at": EARLIER},
        ]
    )

    result = telemetry.deduplicate(frame, ("run_id",))

    assert result.iloc[0]["status"] == "Completed"


def test_distinct_rows_are_all_kept():
    storage.write(
        "job_runs",
        [{"run_id": "r1"}, {"run_id": "r2"}, {"run_id": "r3"}],
    )

    assert len(telemetry.load("job_runs")) == 3


# --- Composite keys --------------------------------------------------------


def test_a_commit_is_identified_by_workspace_table_and_version():
    """Version 7 exists in every table, and table names are stored short, so
    neither column identifies a commit on its own."""
    storage.write(
        "delta_commits",
        [
            {"workspace_name": "w1", "table_name": "ledger", "version": 7, "rows_added": 10},
            {"workspace_name": "w1", "table_name": "ledger_daily", "version": 7, "rows_added": 20},
            {"workspace_name": "w2", "table_name": "ledger", "version": 7, "rows_added": 30},
        ],
    )

    assert len(telemetry.load("delta_commits")) == 3


def test_the_same_commit_collected_twice_counts_once():
    """This is what would have doubled rows_delivered in silent_zero_row."""
    row = {
        "workspace_name": "w1",
        "table_name": "ledger_daily",
        "version": 7,
        "rows_added": 5000,
    }
    storage.write("delta_commits", [row])
    storage.write("delta_commits", [row])

    result = telemetry.load("delta_commits")

    assert len(result) == 1
    assert result["rows_added"].sum() == 5000


def test_a_refresh_id_collides_across_datasets():
    """refresh_id restarts per dataset, so dataset_id has to be part of it."""
    storage.write(
        "refresh_runs",
        [
            {"dataset_id": "d1", "refresh_id": "1", "status": "Completed"},
            {"dataset_id": "d2", "refresh_id": "1", "status": "Failed"},
        ],
    )

    assert len(telemetry.load("refresh_runs")) == 2


# --- Refusing to guess -----------------------------------------------------


def test_a_renamed_column_is_an_error_not_a_silent_pass():
    """A key column that no longer exists means NATURAL_KEYS is stale. Failing
    loudly is the point — quietly skipping dedup would restore the doubling
    bug with nothing to show it had happened."""
    frame = pd.DataFrame([{"id": "r1"}])

    with pytest.raises(KeyError, match="NATURAL_KEYS"):
        telemetry.deduplicate(frame, ("run_id",))


def test_a_dataset_with_no_declared_key_is_returned_as_stored():
    """Guessing a key risks discarding real rows. Duplicates are visible in a
    count; a dropped row is not."""
    storage.write("some_new_dataset", [{"a": 1}, {"a": 1}])

    assert len(telemetry.load("some_new_dataset")) == 2


def test_a_frame_without_collected_at_drops_only_exact_duplicates():
    """No ordering column means no basis for preferring one row over another,
    so the function claims only what it can defend."""
    frame = pd.DataFrame([{"run_id": "r1"}, {"run_id": "r1"}])

    assert len(telemetry.deduplicate(frame, ("run_id",))) == 1


def test_an_empty_dataset_is_empty_not_an_error():
    assert telemetry.load("job_runs").empty


# --- Reporting -------------------------------------------------------------


def test_counts_show_stored_against_deduplicated():
    """Printed at the top of a detection run so a sudden gap is visible."""
    storage.write("job_runs", [{"run_id": "r1"}])
    storage.write("job_runs", [{"run_id": "r1"}])

    assert telemetry.counts()["job_runs"] == (2, 1)


def test_counts_covers_every_declared_dataset():
    """A new dataset added to NATURAL_KEYS must appear in the report without
    anyone remembering to add it there too."""
    assert set(telemetry.counts()) == set(telemetry.NATURAL_KEYS)


# --- Timezones (D-007) -----------------------------------------------------


def test_naive_timestamps_become_utc_aware():
    """Spark strips the timezone on the way back out of Delta. Every rule
    compares run times against an aware cutoff, so this has to be fixed before
    the frame reaches one."""
    frame = pd.DataFrame(
        [{"run_id": "r1", "start_time": datetime(2026, 10, 7, 12, 0)}]
    )

    result = telemetry.as_utc(frame)

    assert result["start_time"].dt.tz is not None


def test_normalising_does_not_shift_the_instant():
    """The stored value was UTC, so labelling it UTC must not move it. Add an
    offset here and every incident's window would be wrong by hours."""
    frame = pd.DataFrame(
        [{"run_id": "r1", "start_time": datetime(2026, 10, 7, 12, 0)}]
    )

    result = telemetry.as_utc(frame)

    assert result["start_time"].iloc[0].hour == 12


def test_an_already_aware_column_is_left_as_utc():
    frame = pd.DataFrame([{"run_id": "r1", "start_time": EARLIER}])

    result = telemetry.as_utc(frame)

    assert result["start_time"].iloc[0] == EARLIER


def test_non_timestamp_columns_are_untouched():
    frame = pd.DataFrame([{"run_id": "r1", "rows_added": 5000, "status": "Completed"}])

    result = telemetry.as_utc(frame)

    assert result["rows_added"].iloc[0] == 5000
    assert result["status"].iloc[0] == "Completed"


def test_loaded_timestamps_are_comparable_with_complete_until():
    """The whole point, stated as the thing that used to raise a TypeError."""
    storage.write(
        "job_runs", [{"run_id": "r1", "end_time": datetime(2026, 10, 7, 9, 0)}]
    )
    storage.write(
        "delta_commits",
        [
            {
                "workspace_name": "h1-finance-prod",
                "table_name": "ledger_daily",
                "version": 1,
                "collected_at": LATER,
            }
        ],
    )

    runs = telemetry.load("job_runs")
    cutoff = telemetry.complete_until(telemetry.load("delta_commits"))

    # No exception, and the comparison means what it says.
    assert (runs["end_time"] < cutoff).all()


# --- How far the record can be trusted ------------------------------------


def test_complete_until_lags_the_latest_collection():
    """Ten minutes behind the most recent collection, so the newest pipeline
    runs are not judged before their commits have been looked for."""
    frame = pd.DataFrame([{"version": 1, "collected_at": LATER}])

    assert telemetry.complete_until(frame) == LATER - timedelta(minutes=10)


def test_complete_until_uses_the_newest_of_several_collections():
    frame = pd.DataFrame(
        [{"version": 1, "collected_at": EARLIER}, {"version": 2, "collected_at": LATER}]
    )

    assert telemetry.complete_until(frame) == LATER - timedelta(minutes=10)


def test_complete_until_is_always_timezone_aware():
    """Delta can return a naive timestamp depending on the Spark session's
    timezone. Comparing naive against aware raises, which is D-007 — the
    single most repeated error in this project."""
    frame = pd.DataFrame([{"version": 1, "collected_at": datetime(2026, 10, 7, 12, 0)}])

    assert telemetry.complete_until(frame).tzinfo is not None


def test_complete_until_on_an_empty_store_is_in_the_past():
    """No commits collected yet. Returning 'now' would let the rule judge runs
    whose commits nobody has looked for, which is a false positive every time."""
    result = telemetry.complete_until(pd.DataFrame())

    assert result < datetime.now(timezone.utc)


# --- The behaviour detection depends on -----------------------------------


def test_deduplicated_commits_do_not_change_a_healthy_sum():
    """The regression this module exists for. Two collections of one 5,000-row
    commit must read as 5,000 delivered, not 10,000 — otherwise a partial
    delivery below a minimum_rows threshold looks like a full one."""
    row = {
        "workspace_name": "h1-finance-prod",
        "table_name": "ledger_daily",
        "version": 101,
        "rows_added": 5000,
        "collected_at": EARLIER,
    }
    storage.write("delta_commits", [row])
    storage.write("delta_commits", [{**row, "collected_at": LATER}])

    assert telemetry.load("delta_commits")["rows_added"].sum() == 5000