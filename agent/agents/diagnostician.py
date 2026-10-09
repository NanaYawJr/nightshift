"""The diagnostician.

Takes an incident and its evidence pack, asks the model why, and returns a
`Diagnosis` — or a deterministic fallback when the model cannot be reached or
answers in a way that cannot be checked.

The whole flow is: build a prompt from the pack, ask once, parse JSON, verify
every cited key exists, return. There is no tool-calling loop yet. The evidence
pack is assembled up front by `agent.tools.evidence`, so the model has what it
needs without having to ask for it, and a single call is far easier to reason
about and to score. `docs/ARCHITECTURE.md` describes a seven-tool loop; that is
the plan, not the present state, and the doc overstates what is built.

Four outcomes, all first-class, because the benchmark has to tell them apart:

- a diagnosis that cites valid evidence
- an abstention, where the model says the evidence is insufficient
- a fallback, where the model could not be reached or was rate-limited
- a fallback, where the model answered but cited evidence that does not exist

The fourth is the interesting one. It is the failure mode of an LLM
diagnostician — fluent, plausible, resting on a number nobody measured — and
catching it in code rather than in review is the point of the citation check.
"""

from __future__ import annotations

import json
import re

from agent import model as model_api
from agent.diagnosis import Diagnosis, UncitedDiagnosis, validate
from agent.fallback.template import template_diagnosis
from agent.tools.evidence import EvidencePack
from common.config import settings

SYSTEM = """You are a Microsoft Fabric platform reliability engineer.

You are given measured facts about one incident on a Fabric estate. Explain the
most likely cause.

Rules:
- Use only the facts given. You have no other information about this estate.
- Cite the exact fact keys your explanation depends on.
- If the facts do not support a conclusion, say so by setting cause to
  "undetermined". An honest abstention is a correct answer; a confident guess
  is not.
- Do not propose anything that writes to the estate. Your action is advice for
  a human who will decide.

Reply with JSON only, no commentary, in exactly this shape:

{
  "cause": "one sentence naming the most likely cause",
  "reasoning": "two or three sentences explaining how the facts lead there",
  "confidence": "high" | "medium" | "low",
  "evidence_cited": ["fact.key", "fact.key"],
  "recommended_action": "what a human should check or change first"
}"""


def diagnose(pack: EvidencePack, fault_class: str) -> Diagnosis:
    """Diagnose one incident. Never raises — always returns a Diagnosis.

    Not raising is a deliberate choice about where this runs. A scheduled
    notebook working through 70 incidents must not stop on the one that
    rate-limited, and an incident with a degraded explanation is far more useful
    than a job that died two incidents in.
    """
    prompt = _build_prompt(pack, fault_class)

    try:
        raw = model_api.complete(prompt, system=SYSTEM)
    except model_api.ModelUnavailable as exc:
        return template_diagnosis(pack, reason=f"model unavailable: {exc}")

    try:
        diagnosis = _parse(raw, pack.incident_id)
    except ValueError as exc:
        return template_diagnosis(pack, reason=f"unparseable reply: {exc}")

    try:
        validate(diagnosis, pack.keys())
    except UncitedDiagnosis as exc:
        # The model answered and the answer failed its check. Recorded as a
        # fallback with the reason, because counting this as a diagnosis is
        # exactly how a benchmark flatters itself.
        return template_diagnosis(pack, reason=str(exc))

    return diagnosis


def _build_prompt(pack: EvidencePack, fault_class: str) -> str:
    """The pack plus the one piece of context the pack cannot carry.

    The fault class comes from the detection rule that fired, so the model is
    told what kind of thing this is without being told why it happened. Naming
    the class narrows the search; naming a cause would be putting the answer in
    the question.
    """
    return (
        f"Incident {pack.incident_id}\n"
        f"Detection rule classified this as: {fault_class}\n\n"
        f"Measured facts:\n{pack.as_prompt()}\n\n"
        f"The only fact keys you may cite are the ones above, spelled exactly."
    )


def _parse(raw: str, incident_id: str) -> Diagnosis:
    """Pull a Diagnosis out of the model's reply.

    Models wrap JSON in prose and in markdown fences however firmly they are
    told not to, so the first JSON object in the reply is extracted rather than
    the whole string being parsed. Anything else is a ValueError and the caller
    falls back.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)

    if not match:
        raise ValueError(f"no JSON object in reply: {raw[:200]!r}")

    try:
        payload = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc

    cause = payload.get("cause")

    if not cause:
        raise ValueError("reply has no 'cause'")

    if cause == "undetermined":
        return Diagnosis.unknown(
            incident_id,
            model=settings.model_name,
            reason=payload.get("reasoning", "model declined to diagnose"),
        )

    confidence = payload.get("confidence", "low")

    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    cited = payload.get("evidence_cited", [])

    if not isinstance(cited, list):
        raise ValueError(f"evidence_cited is not a list: {cited!r}")

    return Diagnosis(
        incident_id=incident_id,
        cause=str(cause),
        reasoning=str(payload.get("reasoning", "")),
        confidence=confidence,
        evidence_cited=[str(key) for key in cited],
        recommended_action=str(payload.get("recommended_action", "")),
        model=settings.model_name,
        source="model",
    )