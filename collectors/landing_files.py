"""What the pipeline was given to copy.

The gap this closes is the one the agent found on its own. Asked why a run
delivered nothing, every diagnosis said some version of "check the source" —
correctly, because the evidence pack described the pipeline in detail and the
thing it was copying not at all. A diagnosis can only be as specific as its
evidence, and no amount of prompting makes a model see a file nobody measured.

Three facts per source file, per collection: does it exist, how big is it, and
how many data rows does it hold. The third is the one that matters. A
header-only CSV is 0 data rows and a few hundred bytes, and it is
indistinguishable from a healthy file by every other signal the platform
produces — the copy activity succeeds, in its normal time, with no error
anywhere (D-023).

Unlike the other collectors this one is a sampler, not a backfill. A Delta log
keeps its own history and the REST APIs serve thirty days; a file on disk has
only a present state. Every snapshot missed is gone, which makes the
collection schedule part of the measurement rather than an operational detail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Rows are counted by reading the file, so a source large enough to be
# expensive is not counted. Nothing in this estate approaches it; the guard
# exists so that pointing the collector at a 40 GB file degrades to "size
# known, rows unknown" rather than stalling the hourly job.
MAX_COUNTED_BYTES = 500 * 1024 * 1024


def collect(
    spark: Any,
    fs: Any,
    sources: dict[str, str],
    workspace: str = "",
) -> list[dict]:
    """Snapshot each source file. `sources` maps a short name to a full path.

    `fs` is `notebookutils.fs`, passed in rather than imported so this module
    stays testable outside Fabric — the same reason `spark` is a parameter.

    A missing file is recorded as a row with `exists` False, not skipped. Its
    absence is the finding: a pipeline reading a file that is not there is a
    different fault from one reading a file that is empty, and both look like
    success from the platform's side.
    """
    collected_at = datetime.now(timezone.utc)
    rows: list[dict] = []

    for name, path in sources.items():
        stat = _stat(fs, path)

        if stat is None:
            rows.append(
                {
                    "workspace_name": workspace,
                    "source_name": name,
                    "path": path,
                    "exists": False,
                    "size_bytes": None,
                    "modified_at": None,
                    "data_rows": None,
                    "collected_at": collected_at,
                }
            )
            continue

        rows.append(
            {
                "workspace_name": workspace,
                "source_name": name,
                "path": path,
                "exists": True,
                "size_bytes": stat["size"],
                "modified_at": stat["modified_at"],
                "data_rows": _count_rows(spark, path, stat["size"]),
                "collected_at": collected_at,
            }
        )

    print(f"{len(rows)} sources sampled")
    return rows


def _stat(fs: Any, path: str) -> dict | None:
    """Size and modified time for one file, or None if it is not there.

    Works by listing the parent directory and matching the name. Listing the
    file directly behaves differently across runtimes, and a missing file
    should produce an answer rather than an exception.
    """
    parent, _, filename = path.rpartition("/")

    try:
        entries = fs.ls(parent)
    except Exception as exc:
        print(f"could not list {parent}: {type(exc).__name__}")
        return None

    for entry in entries:
        if entry.name.rstrip("/") != filename:
            continue

        return {
            "size": int(entry.size),
            # Fabric reports modification time as epoch milliseconds.
            "modified_at": datetime.fromtimestamp(
                entry.modifyTime / 1000, tz=timezone.utc
            ),
        }

    return None


def _count_rows(spark: Any, path: str, size_bytes: int) -> int | None:
    """Data rows in the file, excluding the header. None if not counted.

    None means "not measured", never "empty" — the distinction the whole
    project turns on (D-026). A header-only file returns 0, which is a
    measurement and the signature of the fault.
    """
    if size_bytes > MAX_COUNTED_BYTES:
        print(f"not counting {path}: {size_bytes:,} bytes exceeds the limit")
        return None

    try:
        return spark.read.option("header", "true").csv(path).count()
    except Exception as exc:
        print(f"could not count {path}: {type(exc).__name__}")
        return None