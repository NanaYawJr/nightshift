"""Collect the Delta transaction log, with the rows each commit actually added.

Runs in a Fabric notebook, not locally — `DESCRIBE HISTORY` needs Spark:

    import delta_commits
    rows = delta_commits.collect(spark, ["ledger", "ledger_daily"])

Produces the `delta_commits` dataset: one row per commit, with the table, the
version, when it happened, and how many rows it added.

`rows_added` is computed rather than read. `operationMetrics.numOutputRows` is
populated by Spark but never by Data Factory (D-024), so the only reliable
count is the difference between the row count at a version and at the version
before it. Delta time travel makes that possible; it is not cheap, which is why
it happens here, once per commit, rather than inside a detection rule that runs
hourly.

Counting is bounded to the most recent `max_counted` versions. A table with
hundreds of versions would otherwise be scanned hundreds of times, and old
commits are not what detection is looking at.

This replaced a version that recorded only commit timestamps. The rule built on
it assumed an empty copy writes no commit at all. It does: twenty-three
consecutive pipeline runs against a header-only file each produced a Delta
commit, every one of them adding zero rows. A rule checking only that a commit
existed reported a healthy estate for twenty-three hours while nothing was
delivered. See D-026.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

LAG_MINUTES = 10
MAX_COUNTED_VERSIONS = 60


def collect(
    spark: Any,
    tables: Iterable[str],
    workspace: str = "",
    max_counted: int = MAX_COUNTED_VERSIONS,
) -> list[dict]:
    """Read the transaction log for each table, with row deltas.

    A table that does not exist is skipped rather than raising. An estate loses
    and gains tables, and a collector that stops at the first missing one stops
    collecting everything after it.
    """
    rows: list[dict] = []
    collected_at = datetime.now(timezone.utc)
    tables = list(tables)

    for table in tables:
        try:
            history = spark.sql(f"DESCRIBE HISTORY {table}").collect()
        except Exception as exc:
            print(f"skipped {table}: {type(exc).__name__}")
            continue

        added = _rows_added_by_version(spark, table, history, max_counted)

        for entry in history:
            version = entry["version"]
            rows.append(
                {
                    "workspace_name": workspace,
                    "table_name": table,
                    "version": version,
                    "timestamp": entry["timestamp"],
                    "operation": entry["operation"],
                    # None where not counted (too old) or not countable.
                    "rows_added": added.get(version),
                    # Kept for comparison. NULL for every Data Factory write.
                    "metrics_rows": _metrics_rows(entry),
                    "collected_at": collected_at,
                }
            )

    print(f"{len(rows)} commits across {len(tables)} tables")
    return rows


def _rows_added_by_version(
    spark: Any, table: str, history: list, max_counted: int
) -> dict[int, int | None]:
    """How many rows each recent commit added.

    Counts the table at each version and takes consecutive differences. One
    extra version below the window is counted as a baseline, so the oldest
    version in the window gets a real delta rather than None.
    """
    versions = sorted(entry["version"] for entry in history)

    if not versions:
        return {}

    # Include one extra below the window to serve as the baseline.
    start = max(0, len(versions) - max_counted - 1)
    window = versions[start:]

    counts: dict[int, int | None] = {}
    for version in window:
        counts[version] = _count_at(spark, table, version)

    added: dict[int, int | None] = {}
    for position, version in enumerate(window):
        current = counts[version]

        if current is None:
            added[version] = None
            continue

        if version == 0:
            added[version] = current
            continue

        if position == 0:
            # Baseline itself: no earlier count, so no delta.
            continue

        previous = counts[window[position - 1]]
        added[version] = None if previous is None else current - previous

    return added


def _count_at(spark: Any, table: str, version: int) -> int | None:
    """Row count at a specific version, or None if unreadable.

    A version whose files have been vacuumed cannot be read. That is a gap in
    the record, not an error — returning None lets detection treat it as
    unknown rather than as zero, which would be a false positive.
    """
    try:
        return (
            spark.read.format("delta")
            .option("versionAsOf", version)
            .table(table)
            .count()
        )
    except Exception:
        return None


def _metrics_rows(entry: Any) -> int | None:
    """numOutputRows from operationMetrics, where the writer recorded it.

    Spark populates this; Data Factory does not. Retained only so the gap stays
    visible in the data rather than becoming folklore.
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
    """
    if not rows:
        return datetime.now(timezone.utc) - timedelta(minutes=LAG_MINUTES)

    return rows[0]["collected_at"] - timedelta(minutes=LAG_MINUTES)