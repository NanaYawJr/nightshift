"""What the system says when the model cannot be reached.

Free tiers rate-limit, networks fail, and a scheduled job at 3am cannot wait for
either. The alternative to a fallback is silence, and silence is the exact
failure this project exists to detect — so the one thing the system must never
do is go quiet because its model was busy.

A template diagnosis is honest about what it is. It states what was measured and
what it means, cites the evidence like any other diagnosis, and sets `source` to
"fallback" so the benchmark never counts it as the model's work. It contains no
causal claim beyond what the numbers force, because nothing reasoned about it.

The useful property: it is derived entirely from the evidence pack, so it is
available for every incident, always, at no cost.
"""

from __future__ import annotations

from agent.diagnosis import Diagnosis
from agent.tools.evidence import EvidencePack

FALLBACK_MODEL = "deterministic-template"


def template_diagnosis(pack: EvidencePack, reason: str) -> Diagnosis:
    """Build a diagnosis from the evidence pack alone, with no model.

    `reason` is recorded verbatim so a reviewer can see whether the fallback
    fired because of a rate limit, a network failure, or a diagnosis that failed
    the citation check — three quite different things that would otherwise look
    identical in the store.
    """
    facts = pack.facts

    delivered = _value(facts, "run.rows_delivered")
    typical = _value(facts, "table.typical_delivery_rows")
    duration = _value(facts, "run.duration_seconds")
    median = _value(facts, "pipeline.duration_median_seconds")
    stale_hours = _value(facts, "table.hours_since_last_delivery")
    table = _value(facts, "expectation.target_table")

    cited = [
        key
        for key in (
            "run.rows_delivered",
            "run.duration_seconds",
            "pipeline.duration_median_seconds",
            "table.typical_delivery_rows",
            "table.hours_since_last_delivery",
        )
        if key in facts
    ]

    # Stated as an observation, not a cause. "Ran normally and delivered
    # nothing" is a fact; concluding the source file was empty is reasoning,
    # and reasoning is what was unavailable.
    parts = [
        f"A run of this pipeline completed and wrote "
        f"{_number(delivered)} rows to {table or 'its target table'}."
    ]

    if duration is not None and median is not None:
        parts.append(
            f"It took {duration}s against a usual {median}s, so the run itself "
            f"behaved normally."
        )

    if typical is not None:
        parts.append(f"A normal delivery to this table adds about {typical:,} rows.")

    if stale_hours is not None:
        parts.append(f"The table has not received rows for {stale_hours} hours.")

    return Diagnosis(
        incident_id=pack.incident_id,
        cause="not diagnosed — measured facts only",
        reasoning=" ".join(parts),
        confidence="low",
        evidence_cited=cited,
        recommended_action=(
            "check the pipeline's source for this run before changing the "
            "pipeline — a run of normal length that delivered nothing points "
            "upstream, not at the copy itself"
        ),
        model=FALLBACK_MODEL,
        source="fallback",
        fallback_reason=reason,
    )
    # Note: `cause` deliberately does not read "undetermined". That string is
    # reserved for an abstention by the model, which `agent.diagnosis.validate`
    # exempts from the citation check. A fallback does cite, and should be held
    # to the same standard as anything else that cites.


def _value(facts: dict, key: str):
    fact = facts.get(key)
    return fact.value if fact else None


def _number(value) -> str:
    return "an unknown number of" if value is None else f"{value:,}"