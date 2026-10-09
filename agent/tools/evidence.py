"""What the model is allowed to know.

The diagnostician does not get the telemetry. It gets an evidence pack: a fixed
set of named facts, each one a number or a timestamp read out of telemetry, with
a plain-language label.

Two reasons for the indirection.

The first is the project's organising idea. Everything in a pack is *measured* —
reproducible by anyone with the same telemetry. The model's output is *asserted*.
Keeping them in separate objects means the boundary cannot be blurred by
accident, and the citation check in `agent.diagnosis` can verify that every fact
a diagnosis leans on actually exists.

The second is that a diagnosis is only as good as the baseline it is compared
against. "The run took 22 seconds" means nothing. "The run took 22 seconds and
every run of this pipeline for a week has taken 19 to 23" is the whole answer:
the pipeline is doing its normal work, so the fault is not in the pipeline. A
pack is built to make that comparison available rather than leaving the model to
ask for it.

No business data reaches a pack. Item names, durations, row counts and
timestamps only — telemetry about the platform, not the ledger it carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from detection.incident import Incident


@dataclass(frozen=True)
class Fact:
    """One measured value, named so a diagnosis can cite it.

    `label` exists because the model reads the pack as text and a bare key like
    `pipeline.duration_median_seconds` invites misreading. The label says what
    the number is in words; the key is what gets cited.
    """

    key: str
    value: Any
    label: str


@dataclass
class EvidencePack:
    """Everything known about one incident, and nothing else."""

    incident_id: str
    facts: dict[str, Fact] = field(default_factory=dict)

    def add(self, key: str, value: Any, label: str) -> None:
        self.facts[key] = Fact(key=key, value=value, label=label)

    def keys(self) -> set[str]:
        return set(self.facts)

    def as_prompt(self) -> str:
        """The pack as the model sees it: one fact per line, key first.

        Key first on every line so the model has the exact string to cite. A
        diagnosis citing `duration` instead of `run.duration_seconds` fails the
        check in `agent.diagnosis`, and the fix is to make the correct string
        unmissable rather than to loosen the check.
        """
        lines = []

        for fact in self.facts.values():
            value = "unknown" if fact.value is None else fact.value
            lines.append(f"{fact.key} = {value}   ({fact.label})")

        return "\n".join(lines)


def build(
    incident: Incident,
    runs: pd.DataFrame,
    commits: pd.DataFrame,
    now: datetime | None = None,
) -> EvidencePack:
    """Assemble the evidence pack for one incident.

    `runs` and `commits` must come from `detection.telemetry.load`, which
    deduplicates and makes timestamps UTC-aware. Passing raw storage output
    here would double-count and then raise on the first comparison.
    """
    now = now or datetime.now(timezone.utc)
    pack = EvidencePack(incident_id=incident.incident_id)

    pipeline = incident.item_name
    table = incident.measured.get("expected_table")

    _add_incident_facts(pack, incident)
    _add_pipeline_baseline(pack, runs, pipeline)
    _add_table_baseline(pack, commits, table, now)
    _add_concurrency(pack, runs, incident, pipeline)

    return pack


def _add_incident_facts(pack: EvidencePack, incident: Incident) -> None:
    """The facts the detection rule already measured.

    Copied rather than recomputed. If the rule and the pack disagreed about how
    many rows a run delivered, the incident and its explanation would describe
    different events.
    """
    measured = incident.measured

    pack.add("run.id", measured.get("run_id"), "the pipeline run that was flagged")
    pack.add("run.started", measured.get("run_started"), "when the run started, UTC")
    pack.add("run.ended", measured.get("run_ended"), "when the run ended, UTC")
    pack.add(
        "run.duration_seconds",
        measured.get("run_duration_seconds"),
        "how long the run took",
    )
    pack.add(
        "run.status",
        measured.get("run_status"),
        "the status the platform reported for the run",
    )
    pack.add(
        "run.rows_delivered",
        measured.get("rows_delivered"),
        "rows written to the target table during this run",
    )
    pack.add(
        "expectation.minimum_rows",
        measured.get("rows_expected_minimum"),
        "the configured minimum this run was judged against",
    )
    pack.add(
        "expectation.target_table",
        measured.get("expected_table"),
        "the table this pipeline is declared to write to",
    )


def _add_pipeline_baseline(
    pack: EvidencePack, runs: pd.DataFrame, pipeline: str
) -> None:
    """How this pipeline normally behaves.

    The decisive comparison for a silent failure. A run that took its normal
    time and reported success has not malfunctioned — whatever is wrong is
    upstream of it, in what it was given to copy. Without the baseline, a model
    has no way to tell that apart from a broken pipeline.
    """
    if runs.empty:
        pack.add("pipeline.runs_observed", 0, "runs of this pipeline in telemetry")
        return

    mine = runs[runs["item_name"] == pipeline]

    pack.add(
        "pipeline.runs_observed",
        len(mine),
        "runs of this pipeline in telemetry",
    )

    if mine.empty:
        return

    durations = pd.to_numeric(mine["duration_seconds"], errors="coerce").dropna()

    if not durations.empty:
        pack.add(
            "pipeline.duration_median_seconds",
            round(float(durations.median()), 1),
            "the usual length of a run of this pipeline",
        )
        pack.add(
            "pipeline.duration_min_seconds",
            round(float(durations.min()), 1),
            "the shortest run of this pipeline observed",
        )
        pack.add(
            "pipeline.duration_max_seconds",
            round(float(durations.max()), 1),
            "the longest run of this pipeline observed",
        )

    if "status" in mine.columns:
        failed = (mine["status"] != "Completed").sum()
        pack.add(
            "pipeline.failed_runs",
            int(failed),
            "runs of this pipeline that did not report success",
        )


def _add_table_baseline(
    pack: EvidencePack, commits: pd.DataFrame, table: str | None, now: datetime
) -> None:
    """What a normal delivery to this table looks like, and when the last one was.

    `hours_since_last_delivery` is the one that states impact. A silent pipeline
    matters because something downstream is stale, and the length of the silence
    is the size of the problem.
    """
    if table is None or commits.empty:
        pack.add("table.commits_observed", 0, "commits to this table in telemetry")
        return

    mine = commits[commits["table_name"] == table]

    pack.add(
        "table.commits_observed", len(mine), "commits to this table in telemetry"
    )

    if mine.empty:
        return

    rows = pd.to_numeric(mine["rows_added"], errors="coerce")

    # Zero-row and unknown-row commits are counted separately on purpose. A
    # commit whose row count could not be read is not evidence of nothing being
    # written, and collapsing the two would turn a gap in the record into a
    # finding (D-026).
    delivered = rows[rows > 0]

    pack.add(
        "table.zero_row_commits",
        int((rows == 0).sum()),
        "commits to this table that added no rows",
    )
    pack.add(
        "table.uncounted_commits",
        int(rows.isna().sum()),
        "commits whose row count could not be determined",
    )

    if not delivered.empty:
        pack.add(
            "table.typical_delivery_rows",
            int(delivered.median()),
            "the usual number of rows a successful delivery adds",
        )
        pack.add(
            "table.deliveries_observed",
            len(delivered),
            "commits to this table that added rows",
        )

    last = _last_delivery(mine, rows)

    if last is not None:
        pack.add(
            "table.last_delivery_at",
            last.isoformat(),
            "when this table last received rows",
        )
        pack.add(
            "table.hours_since_last_delivery",
            round((now - last).total_seconds() / 3600, 1),
            "how long this table has been stale",
        )


def _last_delivery(commits: pd.DataFrame, rows: pd.Series) -> datetime | None:
    """Timestamp of the most recent commit that actually added rows."""
    productive = commits[rows > 0]

    if productive.empty:
        return None

    return pd.to_datetime(productive["timestamp"]).max().to_pydatetime()


def _add_concurrency(
    pack: EvidencePack, runs: pd.DataFrame, incident: Incident, pipeline: str
) -> None:
    """What else was running at the same time.

    Here so that contention can be ruled out rather than ignored. An empty
    answer is a finding: nothing else was running, so the capacity was not
    contended and the cause is specific to this pipeline.
    """
    started = incident.measured.get("run_started")
    ended = incident.measured.get("run_ended")

    if runs.empty or not started or not ended:
        pack.add("estate.concurrent_runs", None, "other items running during this run")
        return

    start = pd.Timestamp(started)
    end = pd.Timestamp(ended)

    overlapping = runs[
        (runs["item_name"] != pipeline)
        & runs["start_time"].notna()
        & runs["end_time"].notna()
        & (runs["start_time"] <= end)
        & (runs["end_time"] >= start)
    ]

    pack.add(
        "estate.concurrent_runs",
        len(overlapping),
        "other items running at the same time as this run",
    )