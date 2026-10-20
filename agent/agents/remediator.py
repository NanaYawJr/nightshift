"""The remediation author.

Takes an incident, its evidence and its diagnosis, and proposes what to change
— or says nothing should be. The output goes to a human, who accepts or
rejects it, and that decision is the only fix-acceptance figure the benchmark
has.

Deliberately given the diagnosis rather than re-deriving a cause. Two models
reasoning independently about the same incident would produce a fix for one
story and an explanation of another, and a reviewer reading both would have no
way to tell which they were approving.

Nothing here writes to the estate. The proposal is text.
"""

from __future__ import annotations

import json
import re

from agent import model as model_api
from agent.diagnosis import Diagnosis
from agent.proposal import Proposal, UncitedProposal, validate
from agent.tools.evidence import EvidencePack
from common.config import settings

SYSTEM = """You are a Microsoft Fabric platform reliability engineer proposing
a fix for an incident that has already been diagnosed.

You are given measured facts and the diagnosis. Propose the smallest change
that would stop this recurring, or stop it going unnoticed.

Rules:
- Use only the facts given, and cite the exact fact keys your reasoning rests on.
- Facts beginning `coverage.` describe what was measured, not the estate.
- Prefer making a silent failure loud over trying to prevent the underlying
  event. A pipeline that fails visibly when its input is empty is usually a
  better change than one that tries to guess what the input should have been.
- If no change is warranted — the cause is outside this estate, or the right
  response is to tell a human and alter nothing — set target to "none". That
  is a correct answer, not a failure to produce one.
- Your change will be read by an engineer who decides whether to apply it.
  Nothing is applied automatically. Write it for them.

Reply with JSON only, no commentary, in exactly this shape:

{
  "target": "pipeline" | "semantic_model" | "notebook" | "upstream" | "none",
  "summary": "one line naming the change",
  "change": "what specifically to alter, concretely enough to act on",
  "rationale": "why this change, in two or three sentences",
  "prevents": "what this would have caught, stated as the incident it stops",
  "risks": "what this change could break or make noisier",
  "confidence": "high" | "medium" | "low",
  "evidence_cited": ["fact.key", "fact.key"]
}"""


def propose(pack: EvidencePack, diagnosis: Diagnosis) -> Proposal:
    """Propose a fix for one diagnosed incident. Never raises.

    An undiagnosed incident gets no proposal — `target="none"` with the reason
    — because a fix for an unexplained symptom is a guess wearing a diff.
    """
    if diagnosis.cause == "undetermined" or diagnosis.source != "model":
        return Proposal.none_warranted(
            pack.incident_id,
            model=settings.model_name,
            reason=(
                "no diagnosis to act on — the cause was not established, so any "
                "change would be a guess about a symptom"
            ),
        )

    prompt = _build_prompt(pack, diagnosis)

    try:
        raw = model_api.complete(prompt, system=SYSTEM)
    except model_api.ModelUnavailable as exc:
        return _unavailable(pack, f"model unavailable: {exc}")

    try:
        proposal = _parse(raw, pack.incident_id)
    except ValueError as exc:
        return _unavailable(pack, f"unparseable reply: {exc}")

    try:
        validate(proposal, pack.keys())
    except UncitedProposal as exc:
        return _unavailable(pack, str(exc))

    return proposal


def _build_prompt(pack: EvidencePack, diagnosis: Diagnosis) -> str:
    return (
        f"Incident {pack.incident_id}\n\n"
        f"Diagnosis: {diagnosis.cause}\n"
        f"Reasoning: {diagnosis.reasoning}\n\n"
        f"Measured facts:\n{pack.as_prompt()}\n\n"
        f"The only fact keys you may cite are the ones above, spelled exactly."
    )


def _unavailable(pack: EvidencePack, reason: str) -> Proposal:
    """No proposal, with the reason recorded.

    There is no template fallback here, unlike diagnosis. A deterministic
    diagnosis can still state measured facts usefully; a deterministic
    remediation would be a canned change applied to a situation nobody
    reasoned about, and a reviewer approving one of those is worse than a
    reviewer seeing nothing.
    """
    proposal = Proposal.none_warranted(
        pack.incident_id, model=settings.model_name, reason=reason
    )
    proposal.source = "fallback"
    proposal.fallback_reason = reason

    return proposal


def _parse(raw: str, incident_id: str) -> Proposal:
    match = re.search(r"\{.*\}", raw, re.DOTALL)

    if not match:
        raise ValueError(f"no JSON object in reply: {raw[:200]!r}")

    try:
        payload = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc

    target = payload.get("target")

    if target not in ("pipeline", "semantic_model", "notebook", "upstream", "none"):
        raise ValueError(f"unknown target: {target!r}")

    if target == "none":
        return Proposal.none_warranted(
            incident_id,
            model=settings.model_name,
            reason=payload.get("rationale", "model judged no change warranted"),
        )

    confidence = payload.get("confidence", "low")

    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    cited = payload.get("evidence_cited", [])

    if not isinstance(cited, list):
        raise ValueError(f"evidence_cited is not a list: {cited!r}")

    return Proposal(
        incident_id=incident_id,
        target=target,
        summary=str(payload.get("summary", "")),
        change=str(payload.get("change", "")),
        rationale=str(payload.get("rationale", "")),
        prevents=str(payload.get("prevents", "")),
        risks=str(payload.get("risks", "")),
        confidence=confidence,
        evidence_cited=[str(key) for key in cited],
        model=settings.model_name,
        source="model",
    )