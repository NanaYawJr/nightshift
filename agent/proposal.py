"""What the agent proposes, and what a reviewer judges.

A `Diagnosis` says why something broke. A `Proposal` says what to change about
it. They are separate types because they fail separately and are scored
separately: a correct diagnosis with an unusable fix is still valuable — the
engineer skips the digging — while a clean fix for the wrong cause is worse
than nothing, because it is convincing.

Nothing here applies anything. A proposal is text and a rationale, and the only
thing that happens to it is a human saying yes or no, recorded in the incident
store. Raising a pull request against the workspace is in the architecture and
is not built; the measurement this supports — would an engineer merge this —
does not need it, and pretending otherwise would put an unearned claim in the
benchmark.

The same citation discipline as `Diagnosis`. A fix that rests on a number
nobody measured is the same failure as an explanation that does, and arguably
worse, because somebody might apply it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Confidence = Literal["high", "medium", "low"]

# Where a proposal would take effect. Named rather than free text because the
# benchmark reports acceptance by kind: a reviewer who merges pipeline guards
# but rejects every semantic model change is telling you something specific,
# and free text would hide it.
Target = Literal["pipeline", "semantic_model", "notebook", "upstream", "none"]


class UncitedProposal(ValueError):
    """Raised when a proposal rests on evidence it was never given."""


@dataclass
class Proposal:
    """A change the agent would make, for a human to accept or reject."""

    incident_id: str
    target: Target
    summary: str
    change: str
    rationale: str
    prevents: str
    risks: str
    confidence: Confidence
    evidence_cited: list[str]
    model: str
    source: Literal["model", "abstention", "fallback"] = "model"
    fallback_reason: str = ""
    proposed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["proposed_at"] = self.proposed_at.isoformat()
        return payload

    def __str__(self) -> str:
        return (
            f"{self.incident_id} [{self.confidence}] {self.target}: {self.summary}\n"
            f"  change:   {self.change}\n"
            f"  because:  {self.rationale}\n"
            f"  prevents: {self.prevents}\n"
            f"  risks:    {self.risks}\n"
            f"  cites:    {', '.join(self.evidence_cited) or 'nothing'}"
        )

    @classmethod
    def none_warranted(cls, incident_id: str, model: str, reason: str) -> "Proposal":
        """No change should be made.

        A first-class outcome, not a failure. Plenty of incidents want a human
        told and nothing altered — a source that was legitimately empty, a
        one-off that will not recur. An agent that always has a change to
        suggest is an agent whose suggestions mean nothing.
        """
        return cls(
            incident_id=incident_id,
            target="none",
            summary="no change recommended",
            change="",
            rationale=reason,
            prevents="",
            risks="",
            confidence="low",
            evidence_cited=[],
            model=model,
            source="abstention",
        )


def validate(proposal: Proposal, available: set[str]) -> None:
    """Every cited key must exist in the evidence pack. Raise if not.

    Exempts `target="none"` for the same reason `agent.diagnosis.validate`
    exempts an abstention: declining to propose asserts nothing and has
    nothing to cite.
    """
    if proposal.target == "none":
        return

    unknown = [key for key in proposal.evidence_cited if key not in available]

    if unknown:
        raise UncitedProposal(
            f"{proposal.incident_id} proposes a change citing evidence that does "
            f"not exist: {unknown}."
        )

    if not proposal.evidence_cited:
        raise UncitedProposal(
            f"{proposal.incident_id} proposes a change but cites no evidence. "
            "A reviewer cannot check a fix whose justification is not stated."
        )

    if not proposal.change.strip():
        raise UncitedProposal(
            f"{proposal.incident_id} proposes a change with no change in it. "
            "Use Proposal.none_warranted to recommend doing nothing."
        )