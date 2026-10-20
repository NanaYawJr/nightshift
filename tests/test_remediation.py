"""Remediation tests.

The point of this layer is that a human can judge the output, so the tests are
mostly about what reaches them: a change that cites nothing never does, a
change proposed for an undiagnosed incident never does, and "no change is
warranted" arrives as an answer rather than as silence.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agent import model as model_api
from agent.agents import remediator
from agent.diagnosis import Diagnosis
from agent.proposal import Proposal, UncitedProposal, validate
from agent.tools import evidence
from detection.incident import Incident

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
RUN_START = NOW - timedelta(hours=2)
RUN_END = RUN_START + timedelta(seconds=22)


def an_incident() -> Incident:
    return Incident(
        rule="silent_zero_row",
        fault_class="silent_zero_row",
        workspace="h1-finance-prod",
        item_name="pl_ingest_ledger_daily",
        item_type="DataPipeline",
        severity="high",
        summary="Run completed but wrote no rows.",
        occurrence_key="run-1",
        measured={
            "run_id": "run-1",
            "run_started": RUN_START.isoformat(),
            "run_ended": RUN_END.isoformat(),
            "run_duration_seconds": 22.0,
            "run_status": "Completed",
            "expected_table": "ledger_daily",
            "rows_delivered": 0,
            "rows_expected_minimum": 1,
        },
    )


def some_runs() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "item_name": "pl_ingest_ledger_daily",
                "run_id": f"run-{n}",
                "status": "Completed",
                "start_time": RUN_START - timedelta(hours=n),
                "end_time": RUN_START - timedelta(hours=n) + timedelta(seconds=22),
                "duration_seconds": 22.0,
            }
            for n in range(3)
        ]
    )


def some_commits() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "table_name": "ledger_daily",
                "version": n,
                "rows_added": 5000.0 if n < 2 else 0.0,
                "timestamp": RUN_START - timedelta(hours=3 - n),
            }
            for n in range(3)
        ]
    )


@pytest.fixture
def pack():
    return evidence.build(an_incident(), some_runs(), some_commits(), now=NOW)


def a_diagnosis() -> Diagnosis:
    return Diagnosis(
        incident_id="INC-test",
        cause="the source file contained no data rows",
        reasoning="the run completed normally and delivered nothing",
        confidence="high",
        evidence_cited=["run.rows_delivered"],
        recommended_action="check the upstream export",
        model="test-model",
    )


GOOD_REPLY = """{
  "target": "pipeline",
  "summary": "fail the run when the source has no rows",
  "change": "add a row-count check after the copy activity that fails the pipeline when 0 rows were written",
  "rationale": "the pipeline reports success on an empty input, so the failure is invisible to anyone watching run history",
  "prevents": "a silent zero-row delivery going unnoticed until somebody questions a stale figure",
  "risks": "a legitimately empty source day would now page someone",
  "confidence": "high",
  "evidence_cited": ["run.rows_delivered", "run.status"]
}"""


class FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt, system="", temperature=0.0):
        self.prompts.append(prompt)

        if isinstance(self.reply, Exception):
            raise self.reply

        return self.reply


# --- A usable proposal -----------------------------------------------------


def test_a_clean_reply_becomes_a_proposal(monkeypatch, pack):
    monkeypatch.setattr(model_api, "complete", FakeModel(GOOD_REPLY))

    result = remediator.propose(pack, a_diagnosis())

    assert result.source == "model"
    assert result.target == "pipeline"
    assert "row-count check" in result.change


def test_the_proposal_states_its_risks(monkeypatch, pack):
    """A fix with no stated downside is a fix nobody can weigh."""
    monkeypatch.setattr(model_api, "complete", FakeModel(GOOD_REPLY))

    assert remediator.propose(pack, a_diagnosis()).risks


def test_the_diagnosis_reaches_the_prompt(monkeypatch, pack):
    """The remediator is told the cause rather than re-deriving one. Two
    models reasoning separately would produce a fix for one story and an
    explanation of another."""
    fake = FakeModel(GOOD_REPLY)
    monkeypatch.setattr(model_api, "complete", fake)

    remediator.propose(pack, a_diagnosis())

    assert "the source file contained no data rows" in fake.prompts[0]


# --- What must never reach a reviewer -------------------------------------


def test_an_invented_citation_is_refused(monkeypatch, pack):
    """Worse here than in a diagnosis: somebody might apply it."""
    invented = GOOD_REPLY.replace("run.rows_delivered", "run.cpu_utilisation")
    monkeypatch.setattr(model_api, "complete", FakeModel(invented))

    result = remediator.propose(pack, a_diagnosis())

    assert result.target == "none"
    assert result.source == "fallback"
    assert "does not exist" in result.fallback_reason


def test_a_change_with_no_change_in_it_is_refused():
    proposal = Proposal(
        incident_id="INC-test",
        target="pipeline",
        summary="improve the pipeline",
        change="   ",
        rationale="it should be better",
        prevents="",
        risks="",
        confidence="high",
        evidence_cited=["run.status"],
        model="test-model",
    )

    with pytest.raises(UncitedProposal, match="no change in it"):
        validate(proposal, {"run.status"})


def test_an_uncited_proposal_is_refused():
    proposal = Proposal(
        incident_id="INC-test",
        target="pipeline",
        summary="add a check",
        change="add a row-count check",
        rationale="because",
        prevents="",
        risks="",
        confidence="high",
        evidence_cited=[],
        model="test-model",
    )

    with pytest.raises(UncitedProposal, match="cites no evidence"):
        validate(proposal, {"run.status"})


def test_an_undiagnosed_incident_gets_no_proposal(monkeypatch, pack):
    """A fix for an unexplained symptom is a guess wearing a diff. The model
    is not even called."""
    def fail(*args, **kwargs):
        raise AssertionError("the model must not be asked")

    monkeypatch.setattr(model_api, "complete", fail)

    undetermined = Diagnosis.unknown("INC-test", "test-model", "not enough signal")
    result = remediator.propose(pack, undetermined)

    assert result.target == "none"
    assert "no diagnosis to act on" in result.rationale


def test_a_fallback_diagnosis_gets_no_proposal(monkeypatch, pack):
    """Same reasoning: the template states facts, it does not establish a
    cause, so there is nothing to design a fix against."""
    def fail(*args, **kwargs):
        raise AssertionError("the model must not be asked")

    monkeypatch.setattr(model_api, "complete", fail)

    degraded = a_diagnosis()
    degraded.source = "fallback"

    assert remediator.propose(pack, degraded).target == "none"


# --- Declining to propose --------------------------------------------------


def test_no_change_warranted_is_an_answer_not_a_failure(monkeypatch, pack):
    """An agent that always has a change to suggest is an agent whose
    suggestions mean nothing."""
    reply = '{"target": "none", "rationale": "the source was legitimately empty that day"}'
    monkeypatch.setattr(model_api, "complete", FakeModel(reply))

    result = remediator.propose(pack, a_diagnosis())

    assert result.target == "none"
    assert result.source == "abstention"
    assert "legitimately empty" in result.rationale


def test_declining_needs_no_citations():
    validate(Proposal.none_warranted("INC-test", "m", "nothing to do"), set())


def test_an_unreachable_model_is_a_fallback_not_a_decline(monkeypatch, pack):
    """The distinction the benchmark depends on. There is deliberately no
    template proposal — a canned change applied to a situation nobody
    reasoned about is worse than no change at all."""
    monkeypatch.setattr(
        model_api, "complete", FakeModel(model_api.ModelUnavailable("rate limited"))
    )

    result = remediator.propose(pack, a_diagnosis())

    assert result.source == "fallback"
    assert result.target == "none"
    assert "rate limited" in result.fallback_reason


def test_an_unknown_target_is_refused(monkeypatch, pack):
    """Targets are a fixed set so acceptance can be reported by kind. Free
    text would hide a reviewer who merges pipeline guards and rejects every
    model change."""
    monkeypatch.setattr(
        model_api,
        "complete",
        FakeModel(GOOD_REPLY.replace('"pipeline"', '"the whole estate"')),
    )

    assert remediator.propose(pack, a_diagnosis()).source == "fallback"


def test_propose_never_raises_on_a_bad_reply(monkeypatch, pack):
    monkeypatch.setattr(model_api, "complete", FakeModel("I would suggest, broadly,"))

    assert remediator.propose(pack, a_diagnosis()).target == "none"