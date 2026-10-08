"""One row per real-world thing.

The telemetry store is append-only on purpose (D-028). Every collection run
adds its findings rather than replacing them, so nothing is ever lost and the
record of what was known when survives. The cost of that choice is duplicates:
collect twice and the same pipeline run appears twice.

That is harmless as a record and ruinous as an input. `silent_zero_row` **sums**
`rows_added` across the commits in a run's window, so a duplicated 5,000-row
commit reads as 10,000 delivered; duplicated runs emit the same incident twice.
The first full Fabric run produced exactly this — 336 job_runs where 168 runs
had happened (D-031).

So detection never reads the store directly. It reads through here, which
collapses each dataset to one row per key, keeping the most recently collected
version of it. Last-collected rather than first matters: a run observed as
InProgress and collected again once Completed must resolve to Completed.

The raw store is left untouched. Deduplication is a view, not a cleanup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from collectors.delta_commits import LAG_MINUTES
from common import storage

# Dataset -> the columns that identify one real-world thing.
#
# `delta_commits` includes the workspace because table names are stored short
# (`ledger_daily`, not the four-part Spark name), and two workspaces in an
# estate may each hold a table of that name. Version alone is not unique
# either — version 7 exists in every table.
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "items": ("item_id",),
    "job_runs": ("run_id",),
    "datasets": ("dataset_id",),
    # refresh_id restarts per dataset, so on its own it collides.
    "refresh_runs": ("dataset_id", "refresh_id"),
    "delta_commits": ("workspace_name", "table_name", "version"),
}


def load(dataset: str) -> pd.DataFrame:
    """Read a dataset with duplicates collapsed, newest observation winning.

    A dataset with no declared key is returned as stored. That is deliberate:
    an undeclared key would have to be guessed, and a wrong guess silently
    discards real rows — a worse failure than returning duplicates, because
    duplicates are visible in a count and a dropped row is not.
    """
    frame = storage.read(dataset)

    if frame.empty:
        return frame

    frame = as_utc(frame)

    keys = NATURAL_KEYS.get(dataset)

    if not keys:
        return frame

    return deduplicate(frame, keys)


def as_utc(frame: pd.DataFrame) -> pd.DataFrame:
    """Make every timestamp column timezone-aware UTC.

    Spark writes an aware timestamp to Delta correctly and then hands it back
    to pandas with the timezone stripped, in whatever the session's timezone
    is. The collectors produce aware times, so a frame that has been through
    Delta no longer matches one that has not, and comparing them raises —
    which is D-007, the single most repeated error in this project.

    Normalising here means every reader gets the same thing whether the store
    is local Parquet or a Lakehouse table, and no rule has to think about it.
    """
    if frame.empty:
        return frame

    frame = frame.copy()

    for column in frame.columns:
        values = frame[column]

        if not pd.api.types.is_datetime64_any_dtype(values):
            continue

        if values.dt.tz is None:
            # Naive means it came back through Delta. The stored instant was
            # UTC, so labelling it UTC restores the original moment rather
            # than shifting it.
            frame[column] = values.dt.tz_localize("UTC")
        else:
            frame[column] = values.dt.tz_convert("UTC")

    return frame


def deduplicate(frame: pd.DataFrame, keys: tuple[str, ...]) -> pd.DataFrame:
    """Keep the latest-collected row for each key.

    Separate from `load` so it can be tested without a storage backend, and so
    a caller holding a frame from somewhere else can apply the same rule.
    """
    missing = [key for key in keys if key not in frame.columns]

    if missing:
        raise KeyError(
            f"cannot deduplicate on {keys}: column(s) {missing} not in frame. "
            "A collector's column was renamed without updating NATURAL_KEYS."
        )

    if "collected_at" not in frame.columns:
        # Nothing to order by, so no basis for choosing between two rows with
        # the same key. Dropping exact duplicates is the most that can be
        # claimed honestly.
        return frame.drop_duplicates(subset=list(keys), keep="last")

    # sort_values is stable, so rows collected in the same batch keep their
    # original order and `keep="last"` takes the final one deterministically.
    ordered = frame.sort_values("collected_at", kind="stable")

    return (
        ordered.drop_duplicates(subset=list(keys), keep="last")
        .sort_index()
        .reset_index(drop=True)
    )


def complete_until(commits: pd.DataFrame) -> datetime:
    """The point up to which stored commit data can be trusted as complete.

    The same idea as `delta_commits.complete_until`, which answers it for a
    collection that just ran. This answers it for the store, which is what a
    detection job reads — it has no collection of its own and cannot know when
    the last one happened except by looking.

    The lag constant is imported rather than repeated, so the two cannot drift
    apart and start disagreeing about what "complete" means.
    """
    if commits.empty or "collected_at" not in commits.columns:
        return datetime.now(timezone.utc) - timedelta(minutes=LAG_MINUTES)

    latest = pd.to_datetime(commits["collected_at"]).max()

    # Delta can hand back a naive timestamp depending on the Spark session's
    # timezone setting. Everything downstream compares against tz-aware run
    # times, and comparing the two raises — the error that has now cost this
    # project three separate afternoons (D-007).
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")

    return latest.to_pydatetime() - timedelta(minutes=LAG_MINUTES)


def counts() -> dict[str, tuple[int, int]]:
    """Per dataset: rows stored, rows after deduplication.

    Worth printing at the top of a detection run. A widening gap is normal —
    it is the collection history accumulating. A gap that appears suddenly in
    one dataset means something collected twice, which is usually a schedule
    that overlaps itself.
    """
    summary: dict[str, tuple[int, int]] = {}

    for dataset in NATURAL_KEYS:
        stored = storage.read(dataset)
        summary[dataset] = (len(stored), len(load(dataset)))

    return summary