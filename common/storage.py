"""Where collector output goes.

One interface, two backends. Collectors call `write(dataset, rows)` and never
learn where the rows landed.

Locally that is partitioned Parquet under data/raw — fast, offline, easy to
inspect. Inside a Fabric notebook it is a Delta table in the attached
Lakehouse, which is what lets detection run on a schedule in the same place the
data lives (D-029).

The backend is chosen explicitly rather than detected. A notebook calls
`use_lakehouse(spark)` once at the top; everything else defaults to local. That
keeps the local test suite from ever accidentally depending on Spark, and makes
the notebook's intent visible in its first cell.

Both backends are append-only. D-028: telemetry the collector does not persist
is lost silently, and the local store was deleted once during a schema change,
taking three faults' worth of evidence with it. Nothing here overwrites.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

import pandas as pd

DATA_ROOT = Path("data/raw")


class Backend(Protocol):
    """What a storage backend has to do. Two methods, no state exposed."""

    def write(self, dataset: str, frame: pd.DataFrame) -> str | None: ...

    def read(self, dataset: str) -> pd.DataFrame: ...


class LocalParquet:
    """Partitioned Parquet on the local filesystem. The default."""

    def __init__(self, root: Path = DATA_ROOT):
        self.root = root

    def write(self, dataset: str, frame: pd.DataFrame) -> str | None:
        collected_at = datetime.now(timezone.utc)
        partition = self.root / dataset / f"date={collected_at:%Y-%m-%d}"
        partition.mkdir(parents=True, exist_ok=True)

        # Microseconds and a random suffix, not just the clock time. Two writes
        # in the same second shared a filename and the second silently
        # overwrote the first — the exact loss D-028 is about, in the code
        # meant to prevent it. The suffix also covers two processes colliding.
        stamp = f"{collected_at:%H%M%S_%f}"
        path = partition / f"{stamp}_{uuid.uuid4().hex[:6]}.parquet"

        frame.to_parquet(path, index=False)

        return str(path)

    def read(self, dataset: str) -> pd.DataFrame:
        root = self.root / dataset

        if not root.exists():
            return pd.DataFrame()

        files = sorted(root.glob("date=*/*.parquet"))
        if not files:
            return pd.DataFrame()

        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


class LakehouseDelta:
    """Delta tables in the notebook's attached Lakehouse.

    Appends rather than overwrites, and merges schema so a collector that gains
    a column does not break the table. A collector that *loses* a column leaves
    nulls, which is the right outcome — the gap stays visible in the data.
    """

    def __init__(self, spark: Any, prefix: str = "tel_"):
        self.spark = spark
        # Prefixed so telemetry tables are obviously not estate tables. An
        # agent reading the Lakehouse should not mistake its own observations
        # for the thing it is observing.
        self.prefix = prefix

    def _table(self, dataset: str) -> str:
        return f"{self.prefix}{dataset}"

    def write(self, dataset: str, frame: pd.DataFrame) -> str | None:
        table = self._table(dataset)

        # A column that is null in every row of this batch has no detectable
        # type, so Spark types it VOID and Delta refuses to store it. This is
        # not hypothetical: `datasets.is_refreshable` is None for every row a
        # service principal reads, and `job_runs.failure_reason` is null
        # whenever nothing failed. Dropping the column is the honest option —
        # it carries no information — and mergeSchema adds it back with its
        # real type on the first batch that has a value.
        frame, dropped = _drop_all_null_columns(frame)

        if dropped:
            print(f"{table}: all-null columns omitted from this batch: {dropped}")

        self.spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
        sdf = self.spark.createDataFrame(frame)

        (
            sdf.write.mode("append")
            .option("mergeSchema", "true")
            .format("delta")
            .saveAsTable(table)
        )

        return table

    def read(self, dataset: str) -> pd.DataFrame:
        table = self._table(dataset)

        try:
            return self.spark.table(table).toPandas()
        except Exception:
            # Table not created yet. An empty frame is the honest answer and
            # matches the local backend's behaviour for a missing dataset.
            return pd.DataFrame()


def _drop_all_null_columns(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Return the frame without its entirely-null columns, and their names.

    Kept separate from the backend so it can be tested on its own, and so the
    rule is stated in one place: a column is dropped only when every row in
    this batch is null. One real value anywhere keeps it.
    """
    dropped = [name for name in frame.columns if frame[name].isna().all()]

    if not dropped:
        return frame, []

    return frame.drop(columns=dropped), dropped


_backend: Backend = LocalParquet()


def use_lakehouse(spark: Any, prefix: str = "tel_") -> None:
    """Switch to Delta tables. Called once at the top of a Fabric notebook."""
    global _backend
    _backend = LakehouseDelta(spark, prefix=prefix)


def use_local(root: Path = DATA_ROOT) -> None:
    """Switch back to local Parquet. Mostly for tests."""
    global _backend
    _backend = LocalParquet(root)


def backend_name() -> str:
    """Which backend is active. Worth printing at the start of a collection run
    — writing telemetry to the wrong place is not an error anything reports."""
    return type(_backend).__name__


def write(dataset: str, rows: Sequence[dict[str, Any]]) -> str | None:
    """Append a batch of rows to `dataset`. Returns where they went, or None.

    `collected_at` is stamped here rather than by each collector, so every
    dataset carries it and carries it the same way. Detection depends on it:
    it is how a rule knows how far its evidence can be trusted.
    """
    if not rows:
        return None

    frame = pd.DataFrame(list(rows))

    if "collected_at" not in frame.columns:
        frame["collected_at"] = datetime.now(timezone.utc)

    return _backend.write(dataset, frame)


def read(dataset: str) -> pd.DataFrame:
    """Every row ever collected for this dataset, across all batches."""
    return _backend.read(dataset)