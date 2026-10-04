"""Collect the Delta transaction log for every table in a Lakehouse.

Runs in a Fabric notebook, not locally — `DESCRIBE HISTORY` needs Spark:

    import delta_commits
    delta_commits.collect(spark, ["ledger", "ledger_daily", "repayments"])

Produces the `delta_commits` dataset: one row per commit, with the table, the
version, and when it happened. That is the second half of the silent_zero_row
join — pipeline runs say something ran, commits say something was written, and
the fault lives in the gap between them.

Row counts per version are deliberately not collected here. Counting rows at a
version means reading the data, which on a two-million-row table costs far more
than reading the log. The rule only needs to know whether a commit happened;
how many rows it carried is a question for the agent, once there is a reason to
ask.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

LAG_MINUTES = 10


def collect(spark: Any, tables: Iterable[str], workspace: str = "") -> list[dict]:
    """Read DESCRIBE HISTORY for each table and return one row per commit.

    A table that does not exist is skipped rather than raising. An estate loses
    and gains tables, and a collector that stops at the first missing one stops
    collecting everything after it.
    """
    rows: list[dict] = []
    collected_at = datetime.now(timezone.utc)

    for table in tables:
        try:
            history = spark.sql(f"DESCRIBE HISTORY {table}").collect()
        except Exception as exc:  # table missing, or no history
            print(f"skipped {table}: {type(exc).__name__}")
            continue

        for entry in history:
            rows.append(
                {
                    "workspace_name": workspace,
                    "table_name": table,
                    "version": entry["version"],
                    "timestamp": entry["timestamp"],
                    "operation": entry["operation"],
                    # Kept despite being NULL for Data Factory writes (D-024).
                    # If a future writer populates it, the data is already here.
                    "num_output_rows": _rows_written(entry),
                    "collected_at": collected_at,
                }
            )

    print(f"{len(rows)} commits across {len(list(tables))} tables")
    return rows


def _rows_written(entry: Any) -> int | None:
    """Pull numOutputRows from operationMetrics if the writer recorded it.

    Spark populates this. Data Factory does not — twenty-one consecutive
    pipeline commits returned NULL (D-024). Hence the rule joins on the
    existence of a commit rather than on this value.
    """
    metrics = entry["operationMetrics"]

    if not metrics:
        return None

    value = metrics.get("numOutputRows")
    return int(value) if value is not None else None


def complete_until(rows: list[dict]) -> datetime:
    """The point up to which commit data can be trusted as complete.

    The detection rule needs this. Commits are collected on a schedule, so the
    most recent pipeline runs will have no commit recorded yet — not because
    none happened, but because nobody has looked. Judging those runs would
    produce a false positive on every latest run, every time.

    A margin is subtracted from the collection time because a commit written
    moments before collection may not yet be visible in the log.
    """
    from datetime import timedelta

    if not rows:
        return datetime.now(timezone.utc) - timedelta(minutes=LAG_MINUTES)

    return rows[0]["collected_at"] - timedelta(minutes=LAG_MINUTES)