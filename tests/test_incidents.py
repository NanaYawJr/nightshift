"""Incident store tests.

The interesting ones are about what a later write must *not* destroy. An
append-only store whose read collapses fields is only safe if a human decision
leaves the diagnosis intact and a re-run leaves the decision intact. Both are
tested here because both are silent failures — nothing errors, the row is
simply wrong afterwards.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agent.diagnosis import Diagnosis
from common import storage
from detection.incident import Incident
from store import incidents

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def local_backend(tmp_path):
    storage.use_local(tmp_path)
    yield
    storage.use_local()


def an_incident(run_id: str = "run-1") -> Incident:
    return Incident(
        rule="silent_zero_row",
        fault_class="silent_zero_row",
        workspace="h1-finance-prod",
        item_name="pl_ingest_ledger_daily",
        item_type="DataPipeline",
        severity="high",
        summary="Run completed but wrote no rows.",
        occurrence_key=run_id,
        measured={"run_id": run_id, "rows_delivered": 0, "run_duration_seconds": 22.3},
    )


def a_diagnosis(incident_id: str) -> Diagnosis:
    return Diagnosis(
        incident_id=incident_id,
        cause="the source file contained no data rows",
        reasoning="the run completed normally and the source held 0 rows",
        confidence="high",
        evidence_cited=["run.rows_delivered", "source.data_rows"],
        recommended_action="check the upstream export",
        model="gemini-3.5-flash-lite",
    )


# --- Writing and reading ---------------------------------------------------


def test_an_incident_round_trips():
    incident = an_incident()

    assert incidents.record([incident]) == 1

    stored = incidents.load()
    assert len(stored) == 1
    assert stored.iloc[0]["incident_id"] == incident.incident_id
    assert stored.iloc[0]["item_name"] == "pl_ingest_ledger_daily"


def test_recording_the_same_incident_twice_yields_one_row():
    """Ids are deterministic, so a detection run that repeats yesterday's
    findings must not double the store."""
    incidents.record([an_incident()])
    incidents.record([an_incident()])

    assert len(incidents.load()) == 1


def test_distinct_incidents_are_kept_apart():
    incidents.record([an_incident("run-1"), an_incident("run-2")])

    assert len(incidents.load()) == 2


def test_an_empty_run_writes_nothing():
    assert incidents.record([]) == 0
    assert incidents.load().empty


def test_a_diagnosis_is_stored_beside_its_incident():
    incident = an_incident()
    incidents.record([incident], {incident.incident_id: a_diagnosis(incident.incident_id)})

    row = incidents.load().iloc[0]

    assert row["diagnosis_cause"] == "the source file contained no data rows"
    assert row["diagnosis_confidence"] == "high"
    assert row["diagnosis_model"] == "gemini-3.5-flash-lite"


def test_an_undiagnosed_incident_is_still_stored():
    """Detection runs hourly; diagnosis may not. An incident with no
    diagnosis yet is the normal state, not a broken row."""
    incidents.record([an_incident()])

    assert "diagnosis_cause" not in incidents.load().columns


# --- What a later write must not destroy -----------------------------------


def test_a_decision_does_not_erase_the_diagnosis():
    """The failure this store's design exists to prevent. A reviewer
    approving an incident must not wipe the reasoning they approved."""
    incident = an_incident()
    incidents.record([incident], {incident.incident_id: a_diagnosis(incident.incident_id)})

    incidents.decide(incident.incident_id, "approved", "michael", "looks right")

    row = incidents.load().iloc[0]

    assert row["decision"] == "approved"
    assert row["diagnosis_cause"] == "the source file contained no data rows"
    assert row["reviewer_note"] == "looks right"


def test_re_recording_does_not_erase_a_decision():
    """The same failure in the other direction: detection re-runs hourly and
    must not reset incidents a human has already ruled on."""
    incident = an_incident()
    incidents.record([incident])
    incidents.decide(incident.incident_id, "rejected", "michael")

    incidents.record([incident])

    assert incidents.load().iloc[0]["decision"] == "rejected"


def test_a_later_decision_replaces_an_earlier_one():
    incident = an_incident()
    incidents.record([incident])

    incidents.decide(incident.incident_id, "approved", "michael")
    incidents.decide(incident.incident_id, "superseded", "michael", "duplicate of INC-x")

    row = incidents.load().iloc[0]

    assert row["decision"] == "superseded"
    assert row["reviewer_note"] == "duplicate of INC-x"


def test_a_diagnosis_arriving_later_attaches_to_the_existing_incident():
    """Detection and diagnosis run on different schedules."""
    incident = an_incident()
    incidents.record([incident])
    incidents.record([incident], {incident.incident_id: a_diagnosis(incident.incident_id)})

    stored = incidents.load()

    assert len(stored) == 1
    assert stored.iloc[0]["diagnosis_cause"]


def test_every_observation_survives_in_the_raw_store():
    """The audit trail. The collapsed view answers "what is true now"; the
    raw rows answer "what did we believe, and when"."""
    incident = an_incident()
    incidents.record([incident])
    incidents.decide(incident.incident_id, "approved", "michael")

    assert len(storage.read("incidents")) == 2
    assert len(incidents.load()) == 1


# --- Decisions -------------------------------------------------------------


def test_an_unknown_decision_is_refused():
    """A typo here would quietly create a fourth category that no report
    counts and no benchmark scores."""
    with pytest.raises(ValueError, match="decision must be one of"):
        incidents.decide("INC-x", "aproved", "michael")


def test_an_incident_nobody_has_ruled_on_is_pending():
    incidents.record([an_incident()])

    assert len(incidents.pending()) == 1


def test_a_decided_incident_leaves_the_queue():
    incident = an_incident()
    incidents.record([incident])
    incidents.decide(incident.incident_id, "approved", "michael")

    assert incidents.pending().empty


def test_explicitly_pending_is_still_pending():
    incident = an_incident()
    incidents.record([incident])
    incidents.decide(incident.incident_id, "pending", "michael", "need to check with finance")

    assert len(incidents.pending()) == 1


# --- Blind replay ----------------------------------------------------------


def test_the_benchmark_reference_attaches_after_the_fact():
    """What makes blind replay blind: nothing upstream of scoring can tell a
    synthetic incident from a real one, because the link is written last."""
    incident = an_incident()
    incidents.record([incident], {incident.incident_id: a_diagnosis(incident.incident_id)})

    assert "benchmark_ref" not in incidents.load().columns

    incidents.link_to_fault(incident.incident_id, "silent_zero_row-pl-2cf897-4417")

    row = incidents.load().iloc[0]
    assert row["benchmark_ref"] == "silent_zero_row-pl-2cf897-4417"
    assert row["diagnosis_cause"]


# --- Measured facts --------------------------------------------------------


def test_measured_facts_survive_the_round_trip():
    incidents.record([an_incident()])

    expanded = incidents.measured(incidents.load())

    assert expanded.iloc[0]["rows_delivered"] == 0
    assert expanded.iloc[0]["run_duration_seconds"] == 22.3


def test_measured_is_not_expanded_by_default():
    """Rules measure different things. Flattening every incident into one
    wide frame of mostly-nulls is a presentation choice, not storage."""
    incidents.record([an_incident()])

    assert "measured_json" in incidents.load().columns
    assert "rows_delivered" not in incidents.load().columns


def test_expanding_an_empty_store_is_not_an_error():
    assert incidents.measured(incidents.load()).empty


# --- Summary ---------------------------------------------------------------


def test_the_summary_counts_what_matters():
    first, second = an_incident("run-1"), an_incident("run-2")
    incidents.record([first, second], {first.incident_id: a_diagnosis(first.incident_id)})
    incidents.decide(first.incident_id, "approved", "michael")

    counts = incidents.summarise()

    assert counts["incidents"] == 2
    assert counts["diagnosed"] == 1
    assert counts["by_model"] == 1
    assert counts["pending"] == 1


def test_the_summary_of_an_empty_store_is_zero_not_an_error():
    assert incidents.summarise() == {"incidents": 0}