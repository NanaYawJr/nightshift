"""Harness tests.

Mostly about attribution: which incidents belong to which fault, and which
belong to none. Getting that wrong is how a benchmark quietly credits a rule
for finding something nobody injected, or blames it for missing a fault that
ended before the run it is being judged on.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.diagnosis import Diagnosis
from common import storage
from detection.incident import Incident
from estate.faults.base import GroundTruth, Seal
from eval import harness
from store import incidents as store

NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)
INJECTED = NOW - timedelta(hours=6)
REVERTED = NOW - timedelta(hours=2)


@pytest.fixture(autouse=True)
def local_backend(tmp_path):
    storage.use_local(tmp_path / "data")
    yield
    storage.use_local()


@pytest.fixture
def seal(tmp_path):
    return Seal(tmp_path / "sealed")


def a_truth(fault_id="f-1", item="pl_ingest_ledger_daily", reverted=REVERTED):
    return GroundTruth(
        fault_id=fault_id,
        fault_class="silent_zero_row",
        target_workspace="h1-finance-prod",
        target_item=item,
        mechanism="the landing file was written with its header and no data rows",
        expected_symptom="completed, normal duration, zero rows",
        injected_at=INJECTED,
        seed=4417,
        parameters={},
        reverted_at=reverted,
    )


def an_incident(run_id: str, started: datetime, item="pl_ingest_ledger_daily"):
    return Incident(
        rule="silent_zero_row",
        fault_class="silent_zero_row",
        workspace="h1-finance-prod",
        item_name=item,
        item_type="DataPipeline",
        severity="high",
        summary="Run completed but wrote no rows.",
        occurrence_key=run_id,
        measured={
            "run_id": run_id,
            "run_started": started.isoformat(),
            "rows_delivered": 0,
        },
    )


# --- Attribution -----------------------------------------------------------


def test_an_incident_inside_the_window_is_matched(seal):
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED + timedelta(hours=1))])

    assert len(harness.match(store.load(), seal)["f-1"]) == 1


def test_an_incident_before_the_window_is_not(seal):
    """The fault had not been injected yet, so the rule found something else."""
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED - timedelta(hours=1))])

    assert harness.match(store.load(), seal)["f-1"] == []


def test_an_incident_after_the_revert_is_not(seal):
    """The reason the seal needed an end time at all (D-041). Without one this
    incident would be credited to a fault that had already been undone."""
    seal.store(a_truth())
    store.record([an_incident("r1", REVERTED + timedelta(hours=1))])

    assert harness.match(store.load(), seal)["f-1"] == []


def test_an_incident_on_another_item_is_not(seal):
    """Same window, different pipeline. Time alone does not make it related."""
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED + timedelta(hours=1), item="pl_other")])

    assert harness.match(store.load(), seal)["f-1"] == []


def test_a_live_fault_is_scored_up_to_now(seal):
    """A fault with no end time is still live, and incidents are still arriving
    inside its window."""
    seal.store(a_truth(reverted=None))
    store.record([an_incident("r1", NOW - timedelta(minutes=30))])

    # `now` is passed, not read from the clock — the window of a live fault
    # otherwise depends on when the test happens to run.
    assert len(harness.match(store.load(), seal, now=NOW)["f-1"]) == 1


def test_unmatched_incidents_are_listed_not_counted_as_false_positives(seal):
    """This estate has produced two genuine faults nobody injected. Calling
    everything outside a window a false positive would have scored those as
    the rule being wrong."""
    seal.store(a_truth())
    store.record(
        [
            an_incident("inside", INJECTED + timedelta(hours=1)),
            an_incident("outside", INJECTED - timedelta(days=2)),
        ]
    )

    loose = harness.unmatched(store.load(), seal)

    assert len(loose) == 1


# --- Latency ---------------------------------------------------------------


def test_latency_is_measured_from_injection(seal):
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED + timedelta(hours=2))])

    assert harness.latency("f-1", store.load(), seal) == 2.0


def test_latency_uses_the_first_incident_not_the_last(seal):
    seal.store(a_truth())
    store.record(
        [
            an_incident("r2", INJECTED + timedelta(hours=3)),
            an_incident("r1", INJECTED + timedelta(hours=1)),
        ]
    )

    assert harness.latency("f-1", store.load(), seal) == 1.0


def test_a_missed_fault_has_no_latency(seal):
    """None, not zero. A miss rendered as instant detection is the most
    flattering bug a benchmark can have."""
    seal.store(a_truth())

    assert harness.latency("f-1", store.load(), seal) is None


# --- Results ---------------------------------------------------------------


def test_a_detected_fault_counts_as_detected(seal):
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED + timedelta(hours=1))])

    summary = harness.results(store.load(), seal)

    assert summary["faults_sealed"] == 1
    assert summary["faults_detected"] == 1
    assert summary["faults_missed"] == 0


def test_a_missed_fault_is_reported_as_missed(seal):
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED - timedelta(days=1))])

    summary = harness.results(store.load(), seal)

    assert summary["faults_detected"] == 0
    assert summary["faults_missed"] == 1


def test_results_count_verdicts_once_a_human_has_judged(seal):
    seal.store(a_truth())
    incident = an_incident("r1", INJECTED + timedelta(hours=1))
    store.record([incident])

    store.judge(incident.incident_id, "correct", "michael")

    summary = harness.results(store.load(), seal)

    assert summary["verdict_correct"] == 1
    assert summary["judged_against_seal"] == 1


def test_an_undiagnosed_incident_is_not_a_diagnosis_awaiting_a_verdict(seal):
    """Not as zero correct, and not as a backlog either. The difference
    between "scored badly", "not scored" and "never looked at" is the whole
    credibility of the table (D-047)."""
    seal.store(a_truth())
    store.record([an_incident("r1", INJECTED + timedelta(hours=1))])

    summary = harness.results(store.load(), seal)

    assert summary["undiagnosed"] == 1
    assert summary["diagnosed_awaiting_verdict"] == 0
    assert "verdict_correct" not in summary


# --- Keeping the two kinds of verdict apart (D-047) ------------------------


def test_a_verdict_on_an_unmatched_incident_is_reported_separately(seal):
    """Five correct answers out of five must not read as five out of eleven
    because six other diagnoses were scored with no ground truth to compare
    them against."""
    seal.store(a_truth())
    inside = an_incident("inside", INJECTED + timedelta(hours=1))
    outside = an_incident("outside", INJECTED - timedelta(days=2))
    store.record([inside, outside])

    store.judge(inside.incident_id, "correct", "michael")
    store.judge(outside.incident_id, "restatement", "michael")

    summary = harness.results(store.load(), seal)

    assert summary["verdict_correct"] == 1
    assert summary["unsealed_verdict_restatement"] == 1
    assert "verdict_restatement" not in summary
    assert summary["judged_against_seal"] == 1
    assert summary["judged_without_seal"] == 1


def test_a_diagnosis_with_no_verdict_is_counted_as_awaiting_one(seal):
    seal.store(a_truth())
    incident = an_incident("r1", INJECTED + timedelta(hours=1))
    store.record([incident], {incident.incident_id: _a_diagnosis(incident.incident_id)})

    summary = harness.results(store.load(), seal)

    assert summary["diagnosed_awaiting_verdict"] == 1
    assert summary["undiagnosed"] == 0


def test_a_live_fault_is_reported_as_live(seal):
    """So nobody publishes a table computed while a fault was still running."""
    seal.store(a_truth(reverted=None))

    assert harness.results(store.load(), seal)["faults_still_live"] == 1


def test_a_sealed_fault_with_no_incidents_at_all_is_reported_as_missed(seal):
    """An empty store is a result, not a configuration error: the fault was
    injected and nothing was detected. This must never raise, because a miss
    is the most important thing the harness has to say."""
    seal.store(a_truth())

    summary = harness.results(store.load(), seal)

    assert summary["faults_sealed"] == 1
    assert summary["faults_missed"] == 1
    assert summary["incidents_total"] == 0


# --- Refusing to report nothing (D-045) ------------------------------------


def test_results_refuse_a_seal_that_does_not_exist(tmp_path):
    """The failure this is for: `Seal()` was constructed with a default
    relative path inside a Fabric notebook, and a table of zeros was printed
    and read as a measurement."""
    with pytest.raises(harness.NotConfigured, match="does not exist"):
        harness.results(store.load(), Seal(tmp_path / "nowhere"))


def test_results_refuse_a_seal_directory_that_is_empty(tmp_path):
    """Distinct from the above and just as dangerous — the stray directory the
    old `Seal.__init__` left behind is still sitting there."""
    empty = tmp_path / "sealed"
    empty.mkdir()

    with pytest.raises(harness.NotConfigured, match="is empty"):
        harness.results(store.load(), Seal(empty))


def test_the_table_refuses_an_unconfigured_seal_too(tmp_path):
    """Both reporting surfaces, or the guard is decoration."""
    with pytest.raises(harness.NotConfigured):
        harness.table(store.load(), Seal(tmp_path / "nowhere"))


def test_constructing_a_seal_creates_nothing(tmp_path):
    """The root cause. A Seal that creates its own directory turns a wrong
    path into a plausible-looking empty benchmark."""
    path = tmp_path / "sealed"

    Seal(path)

    assert not path.exists()


def test_storing_a_fault_creates_the_directory(tmp_path):
    """Creation moved to `store`, which is the only method with something to
    put there — so a real injection still works on a clean machine."""
    seal = Seal(tmp_path / "nested" / "sealed")

    seal.store(a_truth())

    assert seal.exists()


def test_results_name_the_backend_and_seal_that_produced_them(seal):
    """An empty store and a store nobody pointed at the Lakehouse give
    identical figures. These two keys are the only way a reader tells them
    apart."""
    seal.store(a_truth())

    summary = harness.results(store.load(), seal)

    assert summary["backend"] == "LocalParquet"
    assert summary["seal_path"] == str(seal.path)


# --- Verdicts --------------------------------------------------------------


def test_restatement_is_a_recordable_verdict(seal):
    """D-044. Added after a diagnosis restated the symptom as the cause —
    fluent, confident, naming no mechanism, and fitting none of the original
    three verdicts."""
    seal.store(a_truth())
    incident = an_incident("r1", INJECTED + timedelta(hours=1))
    store.record([incident])

    store.judge(incident.incident_id, "restatement", "michael", "no mechanism named")

    assert harness.results(store.load(), seal)["verdict_restatement"] == 1


def test_an_unknown_verdict_is_refused(seal):
    seal.store(a_truth())
    incident = an_incident("r1", INJECTED + timedelta(hours=1))
    store.record([incident])

    with pytest.raises(ValueError, match="verdict must be one of"):
        store.judge(incident.incident_id, "nearly", "michael")


def test_a_verdict_and_a_decision_are_recorded_separately(seal):
    """A correct diagnosis can have an unusable fix. Collapsing them would
    hide the most useful thing this measures."""
    seal.store(a_truth())
    incident = an_incident("r1", INJECTED + timedelta(hours=1))
    store.record([incident], {incident.incident_id: _a_diagnosis(incident.incident_id)})

    store.judge(incident.incident_id, "correct", "michael")
    store.decide(incident.incident_id, "rejected", "michael", "not implementable")

    row = store.load().iloc[0]

    assert row["verdict"] == "correct"
    assert row["decision"] == "rejected"
    assert row["diagnosis_cause"]


def _a_diagnosis(incident_id: str) -> Diagnosis:
    return Diagnosis(
        incident_id=incident_id,
        cause="the source file contained no data rows",
        reasoning="normal duration, nothing delivered",
        confidence="high",
        evidence_cited=["run.rows_delivered"],
        recommended_action="check upstream",
        model="test-model",
    )