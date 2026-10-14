"""Tests for the rule that watches the collector.

The cases that matter are the two real outages, replayed as data: a schedule
that died at a known moment, and a dataset that never arrived at all. Both were
missed when they happened, so both are regression tests rather than
hypotheticals.
"""

from datetime import datetime, timedelta, timezone

from detection.rules import collection_gap
from detection.rules.collection_gap import CollectionExpectation

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)

HOURLY = CollectionExpectation(
    dataset="job_runs", every_minutes=60, workspace="h1-nightshift"
)


def minutes_ago(n: int) -> datetime:
    return NOW - timedelta(minutes=n)


# --- Healthy ---------------------------------------------------------------


def test_a_fresh_collection_is_not_an_incident():
    assert collection_gap.detect({"job_runs": minutes_ago(5)}, [HOURLY], NOW) == []


def test_slightly_late_is_not_an_incident():
    """A slow session start or a queued job should not page anyone. One
    interval and a bit is late, not broken."""
    assert collection_gap.detect({"job_runs": minutes_ago(90)}, [HOURLY], NOW) == []


def test_nothing_expected_means_nothing_reported():
    """A dataset with no declared interval is not judged, the same way an
    undeclared write expectation is not judged in silent_zero_row."""
    assert collection_gap.detect({"job_runs": minutes_ago(100_000)}, [], NOW) == []


# --- The outages that actually happened ------------------------------------


def test_the_38_hour_outage_is_caught():
    """8 October: a %pip cell that Fabric disables in scheduled runs. Ten
    failures, the schedule auto-disabled, and detection carried on reporting
    72 incidents against telemetry frozen on the 7th."""
    incidents = collection_gap.detect(
        {"job_runs": NOW - timedelta(hours=38)}, [HOURLY], NOW
    )

    assert len(incidents) == 1
    assert incidents[0].severity == "high"
    assert incidents[0].measured["gap_hours"] == 38.0


def test_the_five_day_outage_is_caught():
    """9 to 14 October: the same schedule, killed by a Delta schema collision."""
    incidents = collection_gap.detect(
        {"delta_commits": NOW - timedelta(days=5)},
        [CollectionExpectation("delta_commits", 60, "h1-nightshift")],
        NOW,
    )

    assert len(incidents) == 1
    assert incidents[0].measured["intervals_missed"] == 120.0


def test_a_dataset_that_never_arrived_is_caught():
    """Distinct from a gap, and arguably worse: every rule reading it sees an
    empty table, which is indistinguishable from a healthy estate."""
    incidents = collection_gap.detect({"job_runs": None}, [HOURLY], NOW)

    assert len(incidents) == 1
    assert incidents[0].severity == "high"
    assert incidents[0].measured["last_collected_at"] is None
    assert "never been collected" in incidents[0].summary


def test_a_dataset_missing_from_the_store_is_the_same_as_never():
    """An absent key and an explicit None mean the same thing."""
    assert len(collection_gap.detect({}, [HOURLY], NOW)) == 1


# --- Severity --------------------------------------------------------------


def test_moderately_behind_is_medium():
    incidents = collection_gap.detect({"job_runs": minutes_ago(180)}, [HOURLY], NOW)

    assert incidents[0].severity == "medium"


def test_badly_behind_is_high():
    incidents = collection_gap.detect({"job_runs": minutes_ago(600)}, [HOURLY], NOW)

    assert incidents[0].severity == "high"


def test_the_interval_is_what_decides_not_the_clock():
    """A daily dataset six hours late is fine; an hourly one is not. The same
    gap means different things."""
    daily = CollectionExpectation("model_stats", 1440, "h1-nightshift")

    assert collection_gap.detect({"model_stats": minutes_ago(360)}, [daily], NOW) == []
    assert collection_gap.detect({"job_runs": minutes_ago(360)}, [HOURLY], NOW) != []


# --- Identity --------------------------------------------------------------


def test_one_outage_yields_one_incident_however_often_detection_runs():
    """Keyed on when the silence began, not on now. An hourly detection job
    filing a fresh incident every hour for one problem is how an alert becomes
    noise and then gets ignored."""
    stopped = NOW - timedelta(hours=10)

    first = collection_gap.detect({"job_runs": stopped}, [HOURLY], NOW)
    later = collection_gap.detect(
        {"job_runs": stopped}, [HOURLY], NOW + timedelta(hours=3)
    )

    assert first[0].incident_id == later[0].incident_id


def test_a_new_outage_is_a_new_incident():
    """Recovered, then stopped again. Same dataset, different event."""
    first = collection_gap.detect({"job_runs": NOW - timedelta(hours=10)}, [HOURLY], NOW)
    second = collection_gap.detect({"job_runs": NOW - timedelta(hours=3)}, [HOURLY], NOW)

    assert first[0].incident_id != second[0].incident_id


def test_two_stalled_datasets_are_two_incidents():
    expectations = [HOURLY, CollectionExpectation("delta_commits", 60, "h1-nightshift")]
    last_seen = {"job_runs": minutes_ago(600), "delta_commits": minutes_ago(600)}

    incidents = collection_gap.detect(last_seen, expectations, NOW)

    assert len({i.incident_id for i in incidents}) == 2


# --- What the incident carries ---------------------------------------------


def test_the_incident_states_only_measured_facts():
    """No claim about why collection stopped. That is the agent's job, and
    conflating the two is how a false alarm becomes unexplainable."""
    incident = collection_gap.detect({"job_runs": minutes_ago(600)}, [HOURLY], NOW)[0]

    assert set(incident.measured) == {
        "dataset",
        "last_collected_at",
        "evaluated_at",
        "gap_hours",
        "expected_every_minutes",
        "intervals_missed",
    }


def test_the_summary_says_why_it_matters():
    """Not that a table is stale — that every result resting on it is."""
    incident = collection_gap.detect({"job_runs": minutes_ago(600)}, [HOURLY], NOW)[0]

    assert "stale evidence" in incident.summary