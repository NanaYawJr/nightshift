"""The incident store.

One table serving four purposes at once: the agent's memory, the audit trail,
the dataset the benchmark scores, and the source for any report. They are the
same rows read four ways, which is the only arrangement where the number in a
report and the number in a benchmark cannot disagree.

**Append-only, like everything else.** A human decision arriving three days
after a diagnosis is not an update to a row — it is another observation of the
same incident, appended, carrying only the fields it knows. Reading collapses
them field by field, newest known value winning, exactly as telemetry does
(D-035). No MERGE, no upsert, no row ever rewritten.

That choice buys the property the audit trail needs: every state an incident
passed through is still in the table, in order, with the time each was written.
"Who approved this and when, and what did the agent say before they did" is a
question the store can answer rather than a question its history has lost.

Three fields and nothing else distinguish what was measured from what was
asserted. `measured` is reproducible from telemetry by anyone. `diagnosis_*`
is one model's opinion on one day. `decision` is a human's. Keeping them in
separate columns is the project's organising idea at the point it finally
touches storage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Sequence

import pandas as pd

from agent.diagnosis import Diagnosis
from agent.proposal import Proposal
from common import storage
from detection.incident import Incident

DATASET = "incidents"

# Decisions a human can record. `pending` is the state every incident starts
# in and most will stay in — an incident nobody has looked at must be
# distinguishable from one that was looked at and accepted.
DECISIONS = ("pending", "approved", "rejected", "superseded")

# How a diagnosis compares to the sealed record. Assigned by a human, because
# "did it name the injected mechanism" is a judgement no string comparison
# makes honestly.
#
# `restatement` is not in the original rubric and was added after the fact. A
# diagnosis that says "the pipeline delivered zero rows" when the incident is
# that the pipeline delivered zero rows names no mechanism at all, so neither
# "wrong mechanism" nor "neither" describes it — and it is the failure that
# looks most like success, being fluent and confident and carrying nothing the
# detection rule had not already produced (D-044).
VERDICTS = ("correct", "partial", "restatement", "incorrect", "abstained")


def record(
    incidents: Iterable[Incident],
    diagnoses: dict[str, Diagnosis] | None = None,
    proposals: dict[str, Proposal] | None = None,
) -> int:
    """Append incidents, with their diagnoses where they exist.

    Returns the number of rows written. Safe to call repeatedly over the same
    incidents: ids are deterministic (`detection.incident`), so re-recording
    produces another observation of the same incident rather than a duplicate
    one, and the read collapses them.
    """
    diagnoses = diagnoses or {}
    proposals = proposals or {}
    rows = [
        _row(
            incident,
            diagnoses.get(incident.incident_id),
            proposals.get(incident.incident_id),
        )
        for incident in incidents
    ]

    if not rows:
        return 0

    storage.write(DATASET, rows)
    return len(rows)


def decide(
    incident_id: str,
    decision: str,
    decided_by: str,
    note: str = "",
) -> None:
    """Record a human's verdict on an incident.

    Appends a row carrying the decision and nothing else. Every other field is
    absent rather than blank, so the read keeps whatever the diagnosis wrote —
    a reviewer approving an incident must not be able to erase the reasoning
    they were approving.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")

    # The id must already exist. Without this check a mistyped id does not
    # fail — it appends a row with no incident attached, and the collapsing
    # read turns that row into an incident of its own. One such row was
    # created by a notebook placeholder reaching this function, and a
    # fabricated incident in a benchmark's results is the worst kind of bug
    # here: it looks exactly like data (D-043).
    known = set(load()["incident_id"]) if not load().empty else set()

    if incident_id not in known:
        raise KeyError(
            f"{incident_id} is not in the incident store, so there is nothing "
            "to decide. Check the id — recording a decision cannot create an "
            "incident."
        )

    storage.write(
        DATASET,
        [
            {
                "incident_id": incident_id,
                "decision": decision,
                "decided_by": decided_by,
                "decided_at": datetime.now(timezone.utc),
                "reviewer_note": note,
            }
        ],
    )


def judge(incident_id: str, verdict: str, judged_by: str, note: str = "") -> None:
    """Record how a diagnosis compared to the sealed ground truth.

    Separate from `decide`, which is about the proposed fix. A diagnosis can
    be correct and its fix unusable, or the reverse, and collapsing the two
    into one verdict would hide the most useful thing the benchmark measures.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, not {verdict!r}")

    known = set(load()["incident_id"]) if not load().empty else set()

    if incident_id not in known:
        raise KeyError(f"{incident_id} is not in the incident store (D-043).")

    storage.write(
        DATASET,
        [
            {
                "incident_id": incident_id,
                "verdict": verdict,
                "judged_by": judged_by,
                "judged_at": datetime.now(timezone.utc),
                "verdict_note": note,
            }
        ],
    )


def link_to_fault(incident_id: str, fault_id: str) -> None:
    """Attach the sealed fault this incident came from.

    Written only by the harness, and only after the diagnosis is complete, so
    nothing upstream of scoring can see that an incident is synthetic. This
    field is what makes blind replay blind.
    """
    known = set(load()["incident_id"]) if not load().empty else set()

    if incident_id not in known:
        raise KeyError(f"{incident_id} is not in the incident store (D-043).")

    storage.write(
        DATASET, [{"incident_id": incident_id, "benchmark_ref": fault_id}]
    )


def load() -> pd.DataFrame:
    """Every incident, one row each, latest known value of every field.

    Reads through `detection.telemetry`, which already implements the
    collapsing rule. The store is a different kind of data from telemetry but
    it has the same shape of problem, and two implementations of one rule is
    how they drift apart.
    """
    from detection import telemetry

    return telemetry.load(DATASET)


def pending() -> pd.DataFrame:
    """Incidents awaiting a human decision — the review queue.

    An incident with no decision row at all is pending; so is one explicitly
    recorded as pending. Both mean nobody has ruled on it.
    """
    frame = load()

    if frame.empty:
        return frame

    if "decision" not in frame.columns:
        return frame

    return frame[frame["decision"].isna() | (frame["decision"] == "pending")]


def _row(
    incident: Incident,
    diagnosis: Diagnosis | None,
    proposal: Proposal | None = None,
) -> dict:
    """One incident as a flat row.

    `measured` and the cited-evidence list are stored as JSON text rather than
    Delta structs. A struct would be tidier to query and far less tidy to
    evolve: `measured` carries whatever a rule chose to measure, and rules are
    still being written. Text costs a `json.loads` at read time and keeps the
    schema from needing a migration every time a rule learns a new fact.
    """
    row = {
        "incident_id": incident.incident_id,
        "detected_at": incident.detected_at,
        "rule": incident.rule,
        "fault_class": incident.fault_class,
        "workspace": incident.workspace,
        "item_name": incident.item_name,
        "item_type": incident.item_type,
        "severity": incident.severity,
        "summary": incident.summary,
        "occurrence_key": incident.occurrence_key,
        "origin": incident.origin,
        "measured_json": json.dumps(incident.measured, default=str),
    }

    if diagnosis is None and proposal is None:
        return row

    if diagnosis is not None:
        row.update(_diagnosis_fields(diagnosis))

    if proposal is not None:
        row.update(_proposal_fields(proposal))

    return row


def _diagnosis_fields(diagnosis: Diagnosis) -> dict:
    return {
            "diagnosis_cause": diagnosis.cause,
            "diagnosis_reasoning": diagnosis.reasoning,
            "diagnosis_confidence": diagnosis.confidence,
            "diagnosis_cited_json": json.dumps(diagnosis.evidence_cited),
            "recommended_action": diagnosis.recommended_action,
            "diagnosis_source": diagnosis.source,
            "diagnosis_model": diagnosis.model,
            "fallback_reason": diagnosis.fallback_reason,
            "diagnosed_at": diagnosis.diagnosed_at,
    }


def _proposal_fields(proposal: Proposal) -> dict:
    """The proposed change, flat. `proposal_target` of "none" is a real
    outcome — the agent judged no change warranted — and is not the same as
    a row with no proposal at all, which means nobody has looked yet."""
    return {
        "proposal_target": proposal.target,
        "proposal_summary": proposal.summary,
        "proposal_change": proposal.change,
        "proposal_rationale": proposal.rationale,
        "proposal_prevents": proposal.prevents,
        "proposal_risks": proposal.risks,
        "proposal_confidence": proposal.confidence,
        "proposal_cited_json": json.dumps(proposal.evidence_cited),
        "proposal_source": proposal.source,
        "proposal_model": proposal.model,
        "proposed_at": proposal.proposed_at,
    }


def measured(frame: pd.DataFrame) -> pd.DataFrame:
    """Expand `measured_json` back into columns, for analysis.

    Deliberately not done in `load`. The stored shape is the honest one — a
    rule's measurements are whatever that rule measured — and flattening them
    into a wide frame of mostly-nulls is a presentation choice that belongs
    where the presenting happens.
    """
    if frame.empty or "measured_json" not in frame.columns:
        return frame

    expanded = pd.json_normalize(
        frame["measured_json"].apply(lambda text: json.loads(text) if text else {})
    )
    expanded.index = frame.index

    return pd.concat([frame.drop(columns=["measured_json"]), expanded], axis=1)


def summarise(frame: pd.DataFrame | None = None) -> dict[str, int]:
    """Counts worth printing after a run: how many, how diagnosed, how decided."""
    frame = load() if frame is None else frame

    if frame.empty:
        return {"incidents": 0}

    counts = {"incidents": len(frame)}

    if "diagnosis_cause" in frame.columns:
        counts["diagnosed"] = int(frame["diagnosis_cause"].notna().sum())

    if "diagnosis_source" in frame.columns:
        counts["by_model"] = int((frame["diagnosis_source"] == "model").sum())
        counts["abstained"] = int((frame["diagnosis_source"] == "abstention").sum())
        counts["by_fallback"] = int((frame["diagnosis_source"] == "fallback").sum())

    if "proposal_target" in frame.columns:
        counts["proposed"] = int(
            frame["proposal_target"].notna().sum()
            - (frame["proposal_target"] == "none").sum()
        )
        counts["no_change_warranted"] = int((frame["proposal_target"] == "none").sum())

    counts["pending"] = len(pending())

    return counts


def to_rows(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    """A readable slice of the store, for printing in a notebook."""
    present = [name for name in columns if name in frame.columns]

    return frame[present].to_string(index=False)