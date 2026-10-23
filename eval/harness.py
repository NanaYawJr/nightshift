"""Scoring a benchmark run.

Matches what the system produced against what was actually done, and reports
the difference. It does not inject anything — faults are injected by hand,
sealed at injection, and closed at revert — because the property that makes
the result credible is the seal, not the automation. An unattended runner
would add ways to go wrong without adding a single thing a reader should
trust more.

The division of labour here is the point.

**Computed, because it is objective.** Whether any incident fell inside a
fault's window. How long after injection the first one appeared. How many
incidents fell outside every window. How many diagnoses came from the model,
how many abstained, how many degraded. Whether a human accepted the fix.

**Recorded, because it is a judgement.** Whether a diagnosis named the
injected mechanism. No string comparison answers that honestly — "the source
file contained no data rows" and "the landing file was written with its header
and no data rows" are the same answer in different words, and a keyword match
would score the project's own wording rather than its correctness. A human
reads the sealed record and the diagnosis and assigns a verdict, which
`store.incidents.judge` writes alongside the incident.

A harness that scored itself would be measuring its own vocabulary.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from common import storage
from estate.faults.base import Seal
from store import incidents as store


class NotConfigured(RuntimeError):
    """Raised when there is no ground truth to score against (D-045).

    A benchmark with no seal has no result — not a result of zero. This is the
    third time this project has had to separate "nothing found" from "nothing
    configured": the first produced 134 NaN row counts read as real zeros, the
    second read a sampler's own coverage as an empty source, and the third
    printed a table of zeros from a seal directory the code had created
    itself. Refusing is cheaper than explaining.
    """

# A run that started before injection but finished after it was still reading
# the pre-fault world for most of its life. Attributing it to the fault would
# inflate detection; ignoring a run that started seconds before the file
# changed would deflate it. The window is applied to the run's start.
ATTRIBUTE_ON = "run_started"


def match(
    frame: pd.DataFrame, seal: Seal, now: datetime | None = None
) -> dict[str, list[str]]:
    """Which incidents fall inside which fault's window.

    An incident belongs to a fault when it concerns the same item and its run
    started while that fault was live. A fault with no end time is treated as
    live up to now, which is correct for scoring a fault still in progress and
    is why `active()` exists to warn you that you are doing that.
    """
    # Passed rather than read, so a run can be scored as of a stated moment
    # and a live fault's open window is a decision rather than a side effect
    # of when the notebook happened to execute.
    now = now or datetime.now(timezone.utc)

    if frame.empty:
        return {fault_id: [] for fault_id in seal.list_ids()}

    expanded = store.measured(frame)
    matched: dict[str, list[str]] = {}

    for fault_id in seal.list_ids():
        truth = seal.load(fault_id)
        started, ended = truth.injected_at, truth.reverted_at
        ended = ended or now

        inside = []

        for _, row in expanded.iterrows():
            if row.get("item_name") != truth.target_item:
                continue

            when = _run_time(row)

            if when is not None and started <= when <= ended:
                inside.append(row["incident_id"])

        matched[fault_id] = inside

    return matched


def unmatched(
    frame: pd.DataFrame, seal: Seal, now: datetime | None = None
) -> list[str]:
    """Incidents inside no fault's window.

    Not the same as false positives, and the difference is worth stating in
    any published figure. An incident here is either a genuine fault nobody
    injected — this estate has produced two — or a rule firing on a healthy
    run. Only a human reading it can say which, which is why this returns ids
    rather than a count labelled "false positives".
    """
    if frame.empty:
        return []

    claimed = {i for ids in match(frame, seal, now).values() for i in ids}

    return [i for i in frame["incident_id"] if i not in claimed]


def latency(
    fault_id: str, frame: pd.DataFrame, seal: Seal, now: datetime | None = None
) -> float | None:
    """Hours from injection to the first incident inside the window.

    None when the fault produced no incidents — a miss, which is a result and
    must not be rendered as a zero.
    """
    inside = match(frame, seal, now).get(fault_id, [])

    if not inside:
        return None

    truth = seal.load(fault_id)
    expanded = store.measured(frame)
    mine = expanded[expanded["incident_id"].isin(inside)]

    first = min(filter(None, (_run_time(row) for _, row in mine.iterrows())), default=None)

    if first is None:
        return None

    return round((first - truth.injected_at).total_seconds() / 3600, 2)


def results(
    frame: pd.DataFrame | None = None,
    seal: Seal | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The benchmark table, as far as it can be computed.

    Verdicts and decisions come from what a human recorded; everything else is
    derived. A key reading `None` means not yet judged, never zero.

    Raises `NotConfigured` when the seal holds no faults, rather than
    returning a summary of zeros (D-045). Reports the active storage backend
    and seal path alongside the figures for the same reason: an unconfigured
    run and a run that found nothing produce the same numbers, and the
    difference has to be visible in the output rather than inferred.
    """
    frame = store.load() if frame is None else frame
    seal = seal or Seal()

    _require_seal(seal)

    matched = match(frame, seal, now)
    detected = {f: ids for f, ids in matched.items() if ids}

    summary: dict[str, Any] = {
        # Which backend the incident numbers came from. Printed because an
        # empty store and a store nobody pointed at the Lakehouse produce
        # identical figures, and only this line tells them apart.
        "backend": storage.backend_name(),
        "seal_path": str(seal.path),
        "faults_sealed": len(matched),
        "faults_detected": len(detected),
        "faults_missed": len(matched) - len(detected),
        "incidents_total": len(frame),
        "incidents_matched": sum(len(ids) for ids in matched.values()),
        "incidents_unmatched": len(unmatched(frame, seal, now)),
        "faults_still_live": len(seal.active()),
    }

    latencies = [
        value
        for value in (latency(f, frame, seal, now) for f in detected)
        if value is not None
    ]

    if latencies:
        ordered = sorted(latencies)
        summary["latency_hours_median"] = ordered[len(ordered) // 2]
        summary["latency_hours_worst"] = ordered[-1]

    summary.update(_counts(frame, "diagnosis_source", "diagnosis"))
    summary.update(_counts(frame, "verdict", "verdict"))
    summary.update(_counts(frame, "decision", "decision"))
    summary.update(_counts(frame, "proposal_target", "proposal"))

    judged = (
        frame["verdict"].notna().sum()
        if not frame.empty and "verdict" in frame.columns
        else 0
    )
    summary["judged"] = int(judged)
    summary["unjudged"] = len(frame) - int(judged)

    return summary


def _require_seal(seal: Seal) -> None:
    """Refuse to report anything when there is no ground truth.

    Guards the two reporting functions rather than `match` and `latency`,
    which are building blocks a caller hands an explicit seal and can
    reasonably call against an empty one. It is the published figure that must
    not be computable from nothing.
    """
    if seal.exists():
        return

    state = "is empty" if seal.path.exists() else "does not exist"

    raise NotConfigured(
        f"No sealed ground truth: {seal.path} {state}. Nothing can be scored "
        "against it, and zeros here would read as measurements. Point Seal at "
        "the directory holding the sealed records — in Fabric that is "
        "Seal('/lakehouse/default/Files/sealed') with lh_nightshift pinned as "
        "the default lakehouse."
    )


def _counts(frame: pd.DataFrame, column: str, prefix: str) -> dict[str, int]:
    if frame.empty or column not in frame.columns:
        return {}

    present = frame[column].dropna()

    return {f"{prefix}_{value}": count for value, count in Counter(present).items()}


def _run_time(row) -> datetime | None:
    """When the incident's run started, as a UTC-aware datetime.

    `measured` is stored as JSON text, so this comes back as a string and has
    to be parsed. Rules that measure something other than a run have no run
    time and are skipped rather than guessed at.
    """
    raw = row.get(ATTRIBUTE_ON)

    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None

    stamp = pd.Timestamp(raw)

    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")

    return stamp.to_pydatetime()


def table(
    frame: pd.DataFrame | None = None,
    seal: Seal | None = None,
    now: datetime | None = None,
) -> str:
    """The per-fault breakdown, as text for a notebook."""
    frame = store.load() if frame is None else frame
    seal = seal or Seal()

    _require_seal(seal)

    # The provenance line is part of the table, not decoration. A reader
    # cannot judge the rows below without knowing which seal and which store
    # produced them.
    lines = [
        f"seal     {seal.path}",
        f"store    {storage.backend_name()}, {len(frame)} "
        f"incident{'' if len(frame) == 1 else 's'}",
        "",
        f"{'fault':<50} {'incidents':>9} {'latency h':>10}  window",
    ]

    for fault_id, ids in match(frame, seal, now).items():
        truth = seal.load(fault_id)
        hours = latency(fault_id, frame, seal, now)
        ended = truth.reverted_at.strftime("%d %b %H:%M") if truth.reverted_at else "LIVE"

        lines.append(
            f"{fault_id:<50} {len(ids):>9} "
            f"{'—' if hours is None else f'{hours:>10.2f}'}  "
            f"{truth.injected_at:%d %b %H:%M} to {ended}"
        )

    return "\n".join(lines)