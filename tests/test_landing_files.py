"""Tests for the source-file sampler.

The case that matters is the header-only file: 0 data rows, a few hundred
bytes, present and readable. Every other signal the platform produces says
healthy, which is why the fault went undetected for 23 hours the first time
(D-023, D-026).
"""

from datetime import datetime, timezone

from collectors import landing_files

PATH = "abfss://ws@onelake.dfs.fabric.microsoft.com/lh.Lakehouse/Files/landing/t.csv"
PARENT = "abfss://ws@onelake.dfs.fabric.microsoft.com/lh.Lakehouse/Files/landing"

# 2026-10-07 14:40:00 UTC, as epoch milliseconds.
MODIFIED_MS = 1791470400000


class FakeEntry:
    def __init__(self, name: str, size: int, modify_time: int):
        self.name = name
        self.size = size
        self.modifyTime = modify_time


class FakeFs:
    """Stands in for notebookutils.fs."""

    def __init__(self, listing: dict[str, list] | None = None, raises: bool = False):
        self.listing = listing or {}
        self.raises = raises

    def ls(self, path):
        if self.raises:
            raise OSError("no such directory")

        return self.listing.get(path, [])


class FakeReader:
    def __init__(self, count: int | None, log: list):
        self._count = count
        self.log = log

    def option(self, key, value):
        self.log.append(("option", key, value))
        return self

    def csv(self, path):
        self.log.append(("csv", path))

        if self._count is None:
            raise ValueError("unreadable")

        return self

    def count(self):
        return self._count


class FakeSpark:
    def __init__(self, count: int | None = 5000):
        self.log: list = []
        self._count = count

    @property
    def read(self):
        return FakeReader(self._count, self.log)


def a_fs(size: int = 250_000) -> FakeFs:
    return FakeFs({PARENT: [FakeEntry("t.csv", size, MODIFIED_MS)]})


# --- The fault -------------------------------------------------------------


def test_a_header_only_file_is_zero_rows_not_missing():
    """The signature of silent_zero_row. The file is present, readable and
    recent; it simply has nothing in it. Nothing else the platform reports
    distinguishes this from a normal day."""
    rows = landing_files.collect(FakeSpark(count=0), a_fs(size=312), {"ledger": PATH})

    assert rows[0]["exists"] is True
    assert rows[0]["data_rows"] == 0
    assert rows[0]["size_bytes"] == 312


def test_a_healthy_file_is_counted():
    rows = landing_files.collect(FakeSpark(count=5000), a_fs(), {"ledger": PATH})

    assert rows[0]["data_rows"] == 5000


# --- Absence ---------------------------------------------------------------


def test_a_missing_file_is_recorded_not_skipped():
    """Its absence is the finding. A pipeline reading a file that is not there
    is a different fault from one reading a file that is empty, and both look
    like success from the platform's side."""
    rows = landing_files.collect(FakeSpark(), FakeFs({PARENT: []}), {"ledger": PATH})

    assert len(rows) == 1
    assert rows[0]["exists"] is False
    assert rows[0]["data_rows"] is None
    assert rows[0]["size_bytes"] is None


def test_an_unlistable_directory_is_recorded_as_missing():
    rows = landing_files.collect(FakeSpark(), FakeFs(raises=True), {"ledger": PATH})

    assert rows[0]["exists"] is False


def test_an_unreadable_file_has_unknown_rows_not_zero():
    """None means not measured, never empty. Reading it as zero would turn a
    gap in the record into a detected fault (D-026)."""
    rows = landing_files.collect(FakeSpark(count=None), a_fs(), {"ledger": PATH})

    assert rows[0]["exists"] is True
    assert rows[0]["size_bytes"] == 250_000
    assert rows[0]["data_rows"] is None


def test_a_file_too_large_to_count_still_reports_its_size():
    """Degrades to "size known, rows unknown" rather than stalling the job."""
    big = landing_files.MAX_COUNTED_BYTES + 1
    rows = landing_files.collect(FakeSpark(), a_fs(size=big), {"ledger": PATH})

    assert rows[0]["size_bytes"] == big
    assert rows[0]["data_rows"] is None


# --- Shape -----------------------------------------------------------------


def test_the_modified_time_is_utc_aware():
    """D-007, for the fifth time. A naive timestamp here raises the moment
    anything compares it to a run time."""
    rows = landing_files.collect(FakeSpark(), a_fs(), {"ledger": PATH})

    assert rows[0]["modified_at"].tzinfo is not None
    assert rows[0]["modified_at"] == datetime.fromtimestamp(
        MODIFIED_MS / 1000, tz=timezone.utc
    )


def test_every_row_carries_the_same_collection_stamp():
    """So one sample is one moment, however many files it covers."""
    fs = FakeFs(
        {
            PARENT: [
                FakeEntry("t.csv", 100, MODIFIED_MS),
                FakeEntry("u.csv", 200, MODIFIED_MS),
            ]
        }
    )
    sources = {"ledger": PATH, "other": PARENT + "/u.csv"}

    rows = landing_files.collect(FakeSpark(), fs, sources)

    assert len({row["collected_at"] for row in rows}) == 1


def test_several_sources_are_all_reported():
    fs = FakeFs(
        {
            PARENT: [
                FakeEntry("t.csv", 100, MODIFIED_MS),
                FakeEntry("u.csv", 200, MODIFIED_MS),
            ]
        }
    )
    sources = {"ledger": PATH, "other": PARENT + "/u.csv"}

    rows = landing_files.collect(FakeSpark(), fs, sources)

    assert {row["source_name"] for row in rows} == {"ledger", "other"}


def test_the_header_is_not_counted_as_a_row():
    """Asserted through the reader options rather than the count, because the
    whole measurement is wrong by one if the header slips in."""
    spark = FakeSpark(count=5000)

    landing_files.collect(spark, a_fs(), {"ledger": PATH})

    assert ("option", "header", "true") in spark.log