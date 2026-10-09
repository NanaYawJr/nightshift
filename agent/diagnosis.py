"""What the model asserts, and the check that keeps it honest.

An `Incident` holds facts. A `Diagnosis` holds a claim about why. Keeping them
as separate types is the project's organising idea expressed in code: the
incident is reproducible from telemetry by anyone, the diagnosis is one model's
opinion on one day, and a report that renders them identically is lying.

The useful part is `validate`. A diagnosis has to cite the evidence it rests on,
by key, and every key has to exist in the pack the model was given. That turns
the most common failure of an LLM-based diagnostician — a fluent explanation
resting on a number nobody measured — from something a reader has to catch into
something the code catches.

It is a weak guarantee and worth being precise about what it is not. It proves
the cited facts were in the pack. It does not prove the reasoning is sound, that
the right facts were cited, or that the conclusion follows. Those are what the
benchmark measures. This only removes the case where there was nothing behind
the claim at all.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Confidence = Literal["high", "medium", "low"]

# A diagnosis that cites nothing is rejected. The model can legitimately fail to
# reach a conclusion, and `Diagnosis.unknown` is how it says so — but an
# uncited assertion is the thing this module exists to stop.
MIN_CITATIONS = 1


class UncitedDiagnosis(ValueError):
    """Raised when a diagnosis leans on evidence it was never given."""


@dataclass
class Diagnosis:
    """One model's account of why an incident happened.

    `cause` is the claim. `reasoning` is how it got there. `evidence_cited` are
    the pack keys it rests on, and `recommended_action` is what it would do —
    stated as a proposal, never applied, because every action goes through the
    human gate.
    """

    incident_id: str
    cause: str
    reasoning: str
    confidence: Confidence
    evidence_cited: list[str]
    recommended_action: str
    model: str
    # "model" when a model produced it, "fallback" when the deterministic
    # template did. Recorded because the benchmark scores them separately and
    # a fallback counted as a diagnosis would flatter the results.
    source: Literal["model", "fallback"] = "model"
    # Why the fallback fired, verbatim. A rate limit, a network failure and a
    # diagnosis that failed the citation check are three quite different
    # problems, and without this they are indistinguishable in the store —
    # which would hide the only one of the three that is the model's fault.
    fallback_reason: str = ""
    diagnosed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["diagnosed_at"] = self.diagnosed_at.isoformat()
        return payload

    def __str__(self) -> str:
        return (
            f"{self.incident_id} [{self.confidence}] {self.cause}\n"
            f"  because: {self.reasoning}\n"
            f"  cites:   {', '.join(self.evidence_cited) or 'nothing'}\n"
            f"  action:  {self.recommended_action}"
        )

    @classmethod
    def unknown(cls, incident_id: str, model: str, reason: str) -> "Diagnosis":
        """A diagnosis that declines to diagnose.

        Needed so that "I could not tell" is a first-class outcome rather than
        something the model has to fake its way around. The benchmark counts
        these as abstentions, which is a far better result than a confident
        wrong answer and has to be recordable as such.
        """
        return cls(
            incident_id=incident_id,
            cause="undetermined",
            reasoning=reason,
            confidence="low",
            evidence_cited=[],
            recommended_action="escalate to a human — the evidence was insufficient",
            model=model,
            source="fallback",
        )


def validate(diagnosis: Diagnosis, available: set[str]) -> None:
    """Check every cited key exists in the evidence pack. Raise if not.

    Raising rather than returning a flag is deliberate. A caller that ignores a
    boolean produces exactly the artefact this is meant to prevent, and the
    caller that should tolerate a bad diagnosis is the one that catches this and
    falls back — which is explicit at the call site in
    `agent.agents.diagnostician`.

    An abstention is exempt: it asserts nothing, so it has nothing to cite.
    """
    if diagnosis.cause == "undetermined":
        return

    unknown = [key for key in diagnosis.evidence_cited if key not in available]

    if unknown:
        raise UncitedDiagnosis(
            f"{diagnosis.incident_id} cites evidence that does not exist: "
            f"{unknown}. The model invented these keys or misspelled them."
        )

    if len(diagnosis.evidence_cited) < MIN_CITATIONS:
        raise UncitedDiagnosis(
            f"{diagnosis.incident_id} asserts a cause but cites no evidence. "
            "An uncited explanation cannot be checked by a reviewer."
        )