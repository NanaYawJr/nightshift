"""Agent tests. No network, no model, no API key.

Three things are being proved here, and only the third is interesting.

The evidence pack contains the right facts and the right baselines. The parser
survives what models actually return rather than what they are told to return.
And a diagnosis that cites a fact nobody measured is rejected — which is the one
guarantee this layer offers, so it is tested from several directions.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agent import model as model_api
from agent.agents import diagnostician
from agent.diagnosis import Diagnosis, UncitedDiagnosis, validate
from agent.fallback.template import template_diagnosis
from agent.tools import evidence
from detection.incident import Incident

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
RUN_START = datetime(2026, 10, 7, 14, 46, tzinfo=timezone.utc)
RUN_END = RUN_START + timedelta(seconds=22)


def an_incident(**overrides) -> Incident:
    measured = {
        "run_id": "run-99",
        "run_started": RUN_START.isoformat(),
        "run_ended": RUN_END.isoformat(),
        "run_duration_seconds": 22.0,
        "run_status": "Completed",
        "expected_table": "ledger_daily",
        "rows_delivered": 0,
        "rows_expected_minimum": 1,
        "last_commit_to_table": RUN_END.isoformat(),
    }
    measured.update(overrides)

    return Incident(
        rule="silent_zero_row",
        fault_class="silent_zero_row",
        workspace="h1-finance-prod",
        item_name="pl_ingest_ledger_daily",
        item_type="DataPipeline",
        severity="high",
        summary="Run completed but wrote no rows.",
        occurrence_key=measured["run_id"],
        measured=measured,
    )


def some_runs() -> pd.DataFrame:
    """Five runs of the flagged pipeline, plus one of something else."""
    rows = [
        {
            "item_name": "pl_ingest_ledger_daily",
            "run_id": f"run-{n}",
            "status": "Completed",
            "start_time": RUN_START - timedelta(hours=n),
            "end_time": RUN_START - timedelta(hours=n) + timedelta(seconds=19 + n),
            "duration_seconds": 19.0 + n,
        }
        for n in range(5)
    ]

    rows.append(
        {
            "item_name": "pl_other",
            "run_id": "other-1",
            "status": "Completed",
            "start_time": RUN_START,
            "end_time": RUN_END,
            "duration_seconds": 22.0,
        }
    )

    return pd.DataFrame(rows)


def some_commits() -> pd.DataFrame:
    """Three real deliveries of 5,000 rows, two empty, one uncountable."""
    rows = [
        {"table_name": "ledger_daily", "version": 1, "rows_added": 5000.0,
         "timestamp": RUN_START - timedelta(hours=5)},
        {"table_name": "ledger_daily", "version": 2, "rows_added": 5000.0,
         "timestamp": RUN_START - timedelta(hours=4)},
        {"table_name": "ledger_daily", "version": 3, "rows_added": 5000.0,
         "timestamp": RUN_START - timedelta(hours=3)},
        {"table_name": "ledger_daily", "version": 4, "rows_added": 0.0,
         "timestamp": RUN_START - timedelta(hours=2)},
        {"table_name": "ledger_daily", "version": 5, "rows_added": 0.0,
         "timestamp": RUN_START},
        {"table_name": "ledger_daily", "version": 6, "rows_added": None,
         "timestamp": RUN_START - timedelta(hours=1)},
        {"table_name": "ledger", "version": 1, "rows_added": 2_000_000.0,
         "timestamp": RUN_START - timedelta(days=6)},
    ]

    return pd.DataFrame(rows)


# --- The evidence pack -----------------------------------------------------


def test_the_pack_carries_the_measured_facts():
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["run.rows_delivered"].value == 0
    assert pack.facts["run.duration_seconds"].value == 22.0
    assert pack.facts["expectation.target_table"].value == "ledger_daily"


def test_the_pack_carries_the_pipeline_baseline():
    """The decisive comparison: 22s against a usual 21s means the run worked."""
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["pipeline.runs_observed"].value == 5
    assert pack.facts["pipeline.duration_median_seconds"].value == 21.0


def test_the_pack_carries_what_a_normal_delivery_looks_like():
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["table.typical_delivery_rows"].value == 5000
    assert pack.facts["table.deliveries_observed"].value == 3


def test_empty_and_uncountable_commits_are_counted_separately():
    """D-026: a commit whose count could not be read is not a commit that
    delivered nothing, and merging them turns a gap into a finding."""
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["table.zero_row_commits"].value == 2
    assert pack.facts["table.uncounted_commits"].value == 1


def test_the_pack_states_how_long_the_table_has_been_stale():
    """Impact, not just cause. The last real delivery was three hours before
    the run, and the pack is built 21h09m after that."""
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["table.hours_since_last_delivery"].value == pytest.approx(
        24.2, abs=0.2
    )


def test_concurrency_is_measured_so_contention_can_be_ruled_out():
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    assert pack.facts["estate.concurrent_runs"].value == 1


def test_a_pack_survives_empty_telemetry():
    """The agent must not crash on the first incident of a fresh estate."""
    pack = evidence.build(an_incident(), pd.DataFrame(), pd.DataFrame(), now=NOW)

    assert pack.facts["pipeline.runs_observed"].value == 0
    assert pack.facts["table.commits_observed"].value == 0


def test_the_prompt_puts_the_key_first_on_every_line():
    """So the model has the exact string to cite. A misspelled key fails the
    citation check, and the fix is to make the right one unmissable."""
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    for line in pack.as_prompt().splitlines():
        assert " = " in line
        assert line.split(" = ")[0] in pack.keys()


def test_no_business_data_reaches_the_pack():
    """Only platform telemetry. This is the property to state when someone asks
    whether an external model can be used on a financial services estate."""
    pack = evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)

    for key in pack.keys():
        assert key.split(".")[0] in {"run", "pipeline", "table", "expectation", "estate"}


# --- The citation check ----------------------------------------------------


def a_diagnosis(**overrides) -> Diagnosis:
    fields = {
        "incident_id": "INC-test",
        "cause": "the source file contained only a header",
        "reasoning": "the run took its usual time and delivered nothing",
        "confidence": "high",
        "evidence_cited": ["run.duration_seconds", "run.rows_delivered"],
        "recommended_action": "check the landing file",
        "model": "test-model",
    }
    fields.update(overrides)

    return Diagnosis(**fields)


def test_a_diagnosis_citing_real_evidence_passes():
    validate(a_diagnosis(), {"run.duration_seconds", "run.rows_delivered"})


def test_an_invented_fact_is_rejected():
    """The failure mode this layer exists for: a fluent explanation resting on
    a number nobody measured."""
    diagnosis = a_diagnosis(evidence_cited=["run.cpu_utilisation_percent"])

    with pytest.raises(UncitedDiagnosis, match="does not exist"):
        validate(diagnosis, {"run.duration_seconds"})


def test_a_misspelled_fact_is_rejected_too():
    """Not leniently matched. A near-miss key means the model was not reading
    the pack, and loosening the check would hide that."""
    diagnosis = a_diagnosis(evidence_cited=["run.duration"])

    with pytest.raises(UncitedDiagnosis):
        validate(diagnosis, {"run.duration_seconds"})


def test_an_uncited_assertion_is_rejected():
    diagnosis = a_diagnosis(evidence_cited=[])

    with pytest.raises(UncitedDiagnosis, match="cites no evidence"):
        validate(diagnosis, {"run.duration_seconds"})


def test_an_abstention_needs_no_citations():
    """Saying "I cannot tell" asserts nothing, so it has nothing to cite — and
    it is a far better outcome than a confident wrong answer."""
    validate(Diagnosis.unknown("INC-test", "test-model", "not enough signal"), set())


def test_an_abstention_is_recorded_as_a_fallback():
    """So the benchmark never counts it as the model's work."""
    assert Diagnosis.unknown("INC-test", "m", "r").source == "fallback"


# --- Parsing what models actually return ----------------------------------


GOOD_REPLY = """{
  "cause": "the source file contained only a header row",
  "reasoning": "the run took 22s against a usual 21s, so it behaved normally, yet delivered 0 rows where 5000 is typical",
  "confidence": "high",
  "evidence_cited": ["run.duration_seconds", "run.rows_delivered"],
  "recommended_action": "check the landing file for this date"
}"""


class FakeModel:
    """Stands in for the provider. Records what it was asked."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt, system="", temperature=0.0):
        self.prompts.append(prompt)

        if isinstance(self.reply, Exception):
            raise self.reply

        return self.reply


@pytest.fixture
def pack():
    return evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)


def test_a_clean_reply_becomes_a_diagnosis(monkeypatch, pack):
    monkeypatch.setattr(model_api, "complete", FakeModel(GOOD_REPLY))

    result = diagnostician.diagnose(pack, "silent_zero_row")

    assert result.source == "model"
    assert "header" in result.cause
    assert result.confidence == "high"


def test_json_wrapped_in_prose_and_fences_is_recovered(monkeypatch, pack):
    """Models do this however firmly they are told not to."""
    wrapped = f"Here is my analysis:\n\n```json\n{GOOD_REPLY}\n```\n\nHope that helps."
    monkeypatch.setattr(model_api, "complete", FakeModel(wrapped))

    assert diagnostician.diagnose(pack, "silent_zero_row").source == "model"


def test_an_invented_citation_falls_back_rather_than_being_published(monkeypatch, pack):
    """The whole chain, end to end: the model answers fluently, cites a fact
    that does not exist, and the system refuses to call it a diagnosis."""
    invented = GOOD_REPLY.replace("run.duration_seconds", "run.cpu_utilisation")
    monkeypatch.setattr(model_api, "complete", FakeModel(invented))

    result = diagnostician.diagnose(pack, "silent_zero_row")

    assert result.source == "fallback"
    assert "does not exist" in result.fallback_reason


def test_an_unreachable_model_falls_back(monkeypatch, pack):
    monkeypatch.setattr(
        model_api, "complete", FakeModel(model_api.ModelUnavailable("rate limited"))
    )

    result = diagnostician.diagnose(pack, "silent_zero_row")

    assert result.source == "fallback"
    assert "rate limited" in result.fallback_reason


def test_unparseable_output_falls_back(monkeypatch, pack):
    monkeypatch.setattr(model_api, "complete", FakeModel("I'm afraid I can't do that."))

    assert diagnostician.diagnose(pack, "silent_zero_row").source == "fallback"


def test_diagnose_never_raises(monkeypatch, pack):
    """A scheduled run working through 70 incidents must not die on one."""
    monkeypatch.setattr(model_api, "complete", FakeModel(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        # Documents the one exception that is NOT swallowed: an error that is
        # not ModelUnavailable is a bug in this project, not a provider
        # problem, and hiding it would make it unfindable.
        diagnostician.diagnose(pack, "silent_zero_row")


def test_an_abstaining_model_is_not_turned_into_a_cause(monkeypatch, pack):
    reply = '{"cause": "undetermined", "reasoning": "the facts are consistent with several causes"}'
    monkeypatch.setattr(model_api, "complete", FakeModel(reply))

    result = diagnostician.diagnose(pack, "silent_zero_row")

    assert result.cause == "undetermined"
    assert "several causes" in result.reasoning


def test_the_fault_class_reaches_the_prompt_but_the_cause_does_not(monkeypatch, pack):
    """Naming the class narrows the search. Naming a cause would be putting the
    answer in the question."""
    fake = FakeModel(GOOD_REPLY)
    monkeypatch.setattr(model_api, "complete", fake)

    diagnostician.diagnose(pack, "silent_zero_row")

    assert "silent_zero_row" in fake.prompts[0]
    assert "header" not in fake.prompts[0]


# --- The fallback ----------------------------------------------------------


def test_the_fallback_cites_evidence_like_any_other_diagnosis(pack):
    result = template_diagnosis(pack, reason="testing")

    validate(result, pack.keys())
    assert result.evidence_cited


def test_the_fallback_states_facts_without_claiming_a_cause(pack):
    result = template_diagnosis(pack, reason="testing")

    assert result.source == "fallback"
    assert "not diagnosed" in result.cause
    assert "0 rows" in result.reasoning


def test_the_fallback_works_on_a_pack_with_almost_nothing_in_it():
    """It is the path that runs when everything else has failed, so it cannot
    be the thing that raises."""
    pack = evidence.build(an_incident(), pd.DataFrame(), pd.DataFrame(), now=NOW)

    result = template_diagnosis(pack, reason="testing")

    assert result.source == "fallback"
    validate(result, pack.keys())