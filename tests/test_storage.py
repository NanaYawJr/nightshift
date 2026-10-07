"""Storage backend tests.

The local backend is tested against real temporary files. The Lakehouse backend
is tested against a fake Spark — these check which operations it asks for, not
whether Delta executes them, which only Fabric can confirm.

The point of these tests is the seam itself: collectors must behave identically
whichever backend is active, because the same collector code runs locally and
in Fabric.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from common import storage


@pytest.fixture(autouse=True)
def local_backend(tmp_path):
    """Every test starts on a clean local backend in its own directory."""
    storage.use_local(tmp_path)
    yield
    storage.use_local()


ROWS = [
    {"run_id": "r1", "status": "Completed", "duration_seconds": 22.0},
    {"run_id": "r2", "status": "Completed", "duration_seconds": 21.5},
]


class FakeWriter:
    def __init__(self, log: list):
        self.log = log

    def mode(self, value):
        self.log.append(("mode", value))
        return self

    def option(self, key, value):
        self.log.append(("option", key, value))
        return self

    def format(self, value):
        self.log.append(("format", value))
        return self

    def saveAsTable(self, name):
        self.log.append(("saveAsTable", name))


class FakeSparkFrame:
    def __init__(self, log: list, pdf: pd.DataFrame | None = None):
        self.log = log
        self._pdf = pdf if pdf is not None else pd.DataFrame()

    @property
    def write(self):
        return FakeWriter(self.log)

    def toPandas(self):
        return self._pdf


class FakeConf:
    def __init__(self, log: list):
        self.log = log

    def set(self, key, value):
        self.log.append(("conf", key, value))


class FakeSpark:
    """Records what was asked of it. Executes nothing."""

    def __init__(self, tables: dict[str, pd.DataFrame] | None = None):
        self.log: list[tuple] = []
        self.conf = FakeConf(self.log)
        self._tables = tables or {}

    def createDataFrame(self, pdf):
        self.log.append(("createDataFrame", len(pdf)))
        return FakeSparkFrame(self.log)

    def table(self, name):
        self.log.append(("table", name))
        if name not in self._tables:
            raise ValueError(f"no such table: {name}")
        return FakeSparkFrame(self.log, self._tables[name])


# --- Local backend ---------------------------------------------------------


def test_write_then_read_round_trips():
    storage.write("job_runs", ROWS)
    frame = storage.read("job_runs")

    assert len(frame) == 2
    assert set(frame["run_id"]) == {"r1", "r2"}


def test_writing_twice_appends_rather_than_replacing():
    """D-028: nothing here overwrites. Telemetry lost is lost silently."""
    storage.write("job_runs", ROWS)
    storage.write("job_runs", [{"run_id": "r3", "status": "Completed"}])

    assert len(storage.read("job_runs")) == 3


def test_collected_at_is_stamped_on_every_row():
    """Detection needs it — it is how a rule knows how far evidence goes."""
    storage.write("job_runs", ROWS)
    frame = storage.read("job_runs")

    assert "collected_at" in frame.columns
    assert frame["collected_at"].notna().all()


def test_a_collector_supplied_collected_at_is_kept():
    """delta_commits sets its own, so every commit in one run shares a stamp."""
    stamp = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    storage.write("delta_commits", [{"version": 1, "collected_at": stamp}])

    assert storage.read("delta_commits")["collected_at"].iloc[0] == stamp


def test_empty_rows_write_nothing():
    assert storage.write("job_runs", []) is None
    assert storage.read("job_runs").empty


def test_reading_an_unknown_dataset_is_empty_not_an_error():
    assert storage.read("never_collected").empty


def test_datasets_do_not_leak_into_each_other():
    storage.write("job_runs", ROWS)
    storage.write("refresh_runs", [{"refresh_id": "x"}])

    assert len(storage.read("job_runs")) == 2
    assert len(storage.read("refresh_runs")) == 1


# --- Backend switching -----------------------------------------------------


def test_local_is_the_default():
    assert storage.backend_name() == "LocalParquet"


def test_use_lakehouse_switches_backend():
    storage.use_lakehouse(FakeSpark())
    assert storage.backend_name() == "LakehouseDelta"


def test_collectors_do_not_change_between_backends():
    """The seam's whole purpose: identical calls, different destination."""
    spark = FakeSpark()
    storage.use_lakehouse(spark)

    storage.write("job_runs", ROWS)

    assert ("saveAsTable", "tel_job_runs") in spark.log


# --- Lakehouse backend -----------------------------------------------------


def test_lakehouse_appends_and_merges_schema():
    """Append so history survives; mergeSchema so a new column does not break
    the table."""
    spark = FakeSpark()
    storage.use_lakehouse(spark)

    storage.write("job_runs", ROWS)

    assert ("mode", "append") in spark.log
    assert ("option", "mergeSchema", "true") in spark.log
    assert ("format", "delta") in spark.log


def test_lakehouse_never_overwrites():
    """Regression guard for D-028. An overwrite here loses telemetry."""
    spark = FakeSpark()
    storage.use_lakehouse(spark)

    storage.write("job_runs", ROWS)

    assert ("mode", "overwrite") not in spark.log


def test_lakehouse_tables_are_prefixed():
    """Telemetry must not be mistaken for the estate it observes."""
    spark = FakeSpark()
    storage.use_lakehouse(spark)

    storage.write("job_runs", ROWS)

    assert ("saveAsTable", "tel_job_runs") in spark.log
    assert ("saveAsTable", "job_runs") not in spark.log


def test_lakehouse_read_returns_the_table():
    expected = pd.DataFrame(ROWS)
    spark = FakeSpark(tables={"tel_job_runs": expected})
    storage.use_lakehouse(spark)

    pd.testing.assert_frame_equal(storage.read("job_runs"), expected)


def test_lakehouse_read_of_a_missing_table_is_empty():
    """Matches the local backend. A collector's first run must not crash
    because its table does not exist yet."""
    storage.use_lakehouse(FakeSpark())

    assert storage.read("job_runs").empty


def test_lakehouse_enables_arrow_before_converting():
    """Without Arrow, a large frame converts row by row through Python
    objects and a collection run takes minutes instead of seconds."""
    spark = FakeSpark()
    storage.use_lakehouse(spark)

    storage.write("job_runs", ROWS)

    assert ("conf", "spark.sql.execution.arrow.pyspark.enabled", "true") in spark.log