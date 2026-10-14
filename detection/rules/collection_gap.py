"""Detect when the agent has stopped watching.

Every other rule in this project asks whether the estate is healthy. This one
asks whether the answer can be trusted, and it exists because twice now it
could not be.

On 8 October the collector's notebook hit a `%pip` cell that Fabric disables in
scheduled runs. It failed ten times, Fabric auto-disabled the schedule, and for
38 hours detection went on reporting 72 incidents with precise boundaries and
the agent diagnosed six of them with confidence — all against telemetry frozen
on the 7th. On 9 October the same schedule died again, this time on a Delta
schema collision, and nothing was collected for five days.

Neither outage was detected. Both were noticed by a human comparing numbers
against memory. A monitoring system that cannot see its own silence is not a
monitoring system; it is a report that happens to have been true once.

D-028 predicted this in a closing note — "a detection rule over the collectors
would have caught this" — and it was not built, which is why it happened twice.

The signal is the same shape as every other rule here: an absence where
something was expected. The difference is that the absence is in the evidence
itself rather than in the estate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from detection.incident import Incident

RULE = "collection_gap"
FAULT_CLASS = "collection_gap"

# A collection can be a little late without anything being wrong — a slow
# session start, a queued job. Two intervals is late; six is broken.
LATE_AFTER_INTERVALS = 2.0
BROKEN_AFTER_INTERVALS = 6.0


@dataclass(frozen=True)
class CollectionExpectation:
    """A declaration that a dataset is collected on a schedule.

    Configuration, not telemetry — the same reasoning as `WriteExpectation`
    (D-025). Nothing in the store records how often a dataset is *supposed* to
    arrive, only when it last did, and a dataset that has never been collected
    is indistinguishable from one that was never meant to be.
    """

    dataset: str
    every_minutes: int
    workspace: str


def detect(
    last_collected: dict[str, datetime | None],
    expectations: Iterable[CollectionExpectation],
    now: datetime,
) -> list[Incident]:
    """Find datasets whose collection has stopped or fallen behind.

    `last_collected` maps dataset name to the newest `collected_at` in the
    store, or None where the dataset has never been written —
    `telemetry.last_collected()` produces it.

    `now` is passed rather than read so the rule stays a pure function and can
    be tested at any point in time, which is the same property that makes
    `silent_zero_row` replayable in the benchmark.
    """
    incidents: list[Incident] = []

    for expectation in expectations:
        seen = last_collected.get(expectation.dataset)

        if seen is None:
            incidents.append(_never_collected(expectation, now))
            continue

        gap = now - seen
        interval = timedelta(minutes=expectation.every_minutes)
        intervals_missed = gap / interval

        if intervals_missed < LATE_AFTER_INTERVALS:
            continue

        incidents.append(_gap_incident(expectation, seen, gap, intervals_missed, now))

    return incidents


def _severity(intervals_missed: float) -> str:
    return "high" if intervals_missed >= BROKEN_AFTER_INTERVALS else "medium"


def _gap_incident(
    expectation: CollectionExpectation,
    seen: datetime,
    gap: timedelta,
    intervals_missed: float,
    now: datetime,
) -> Incident:
    hours = gap.total_seconds() / 3600

    return Incident(
        rule=RULE,
        fault_class=FAULT_CLASS,
        workspace=expectation.workspace,
        item_name=expectation.dataset,
        item_type="Telemetry",
        severity=_severity(intervals_missed),
        summary=(
            f"{expectation.dataset} was last collected {hours:.1f} hours ago, "
            f"against an expected every {expectation.every_minutes} minutes. "
            f"Any detection result covering this period is measured against "
            f"stale evidence."
        ),
        # Keyed on when the silence began, not on now. The same outage
        # therefore produces the same incident id however many times detection
        # runs during it — otherwise an hourly job would file a fresh incident
        # every hour for one problem, which is how an alert becomes noise.
        occurrence_key=f"{expectation.dataset}|{seen.isoformat()}",
        measured={
            "dataset": expectation.dataset,
            "last_collected_at": seen.isoformat(),
            "evaluated_at": now.isoformat(),
            "gap_hours": round(hours, 2),
            "expected_every_minutes": expectation.every_minutes,
            "intervals_missed": round(intervals_missed, 1),
        },
    )


def _never_collected(expectation: CollectionExpectation, now: datetime) -> Incident:
    return Incident(
        rule=RULE,
        fault_class=FAULT_CLASS,
        workspace=expectation.workspace,
        item_name=expectation.dataset,
        item_type="Telemetry",
        severity="high",
        summary=(
            f"{expectation.dataset} has never been collected. Every rule that "
            f"reads it is reporting on an empty table, which looks identical "
            f"to a healthy estate."
        ),
        # Constant, because there is no "when it stopped" — it never started.
        occurrence_key=f"{expectation.dataset}|never",
        measured={
            "dataset": expectation.dataset,
            "last_collected_at": None,
            "evaluated_at": now.isoformat(),
            "gap_hours": None,
            "expected_every_minutes": expectation.every_minutes,
            "intervals_missed": None,
        },
    )