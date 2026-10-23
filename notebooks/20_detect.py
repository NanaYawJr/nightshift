# Fabric notebook: 20_detect
#
# Runs the detection rules over the telemetry 10_collect wrote, and reports
# what they found. Reads only; writes nothing. The incidents it produces are
# persisted by 30_diagnose, not here, because a detection run that has not
# been reviewed is not yet a record of anything.
#
# Cell 2 comes first for a reason. Every number this notebook prints is worth
# exactly what the collection behind it is worth, and twice during the build
# that collection had silently stopped while detection went on reporting with
# confidence against frozen telemetry (D-036).
#
# WHERE IT LIVES
#   Workspace:   h1-nightshift
#   Lakehouse:   lh_nightshift, pinned as default
#   Environment: env_nightshift (REQUIRED, D-033)
#   Schedule:    run after 10_collect
#
# Paste each block below into its own notebook cell, in order.


# ============================================================================
# CELL 1 — Fetch the repo
# ============================================================================
#
# Identical in 10_collect, 20_detect, 30_diagnose and 40_review.

import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "NanaYawJr/nightshift"
BRANCH = "main"

work = Path("/tmp/nightshift-detect")
if work.exists():
    shutil.rmtree(work)
work.mkdir(parents=True)

archive = work / "repo.zip"
urllib.request.urlretrieve(
    f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", archive
)
with zipfile.ZipFile(archive) as zf:
    zf.extractall(work)

package_root = next(p for p in work.iterdir() if p.is_dir())

if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))

# Anything already imported from a previous run is stale (D-040), and Python
# will keep using it however many times this cell re-downloads the code. The
# package list is derived from the download rather than written by hand
# (D-048), because a hand-written list omitted `eval` and a notebook spent an
# afternoon scoring with replaced code.
OURS = {p.name for p in package_root.iterdir() if (p / "__init__.py").exists()}

stale = [name for name in sys.modules if name.split(".")[0] in OURS]
for name in stale:
    del sys.modules[name]

try:
    import pydantic_settings  # noqa: F401
except ImportError as exc:
    raise RuntimeError(
        "pydantic-settings is missing. Select the env_nightshift Environment "
        "in the ribbon (D-033)."
    ) from exc

print(f"repo at {package_root}")
print(f"packages: {sorted(OURS)}")
print(f"stale modules cleared: {len(stale)}")


# ============================================================================
# CELL 2 — Is the evidence current?
# ============================================================================
#
# First, deliberately. See the note at the top of this file.

from datetime import datetime, timezone

from common import storage
from detection import telemetry
from detection.rules import collection_gap
from detection.rules.collection_gap import CollectionExpectation

storage.use_lakehouse(spark, prefix="tel_")

# How often each dataset is supposed to arrive. Configuration, not telemetry:
# nothing in the store records what the schedule is meant to be (D-025).
COLLECTION = [
    CollectionExpectation(dataset, every_minutes=60, workspace="h1-nightshift")
    for dataset in (
        "items",
        "job_runs",
        "datasets",
        "refresh_runs",
        "delta_commits",
        "landing_files",
    )
]

gaps = collection_gap.detect(
    telemetry.last_collected(), COLLECTION, datetime.now(timezone.utc)
)

if gaps:
    print("=" * 70)
    print("COLLECTION IS BEHIND — everything below is measured against stale")
    print("evidence. Fix this before believing any of it.")
    print("=" * 70)
    for gap in gaps:
        print(f"  {gap}")
    print()
else:
    print("collection is current")
    print()

# `stored` against `unique` is the duplication the append-only store accepts
# by design (D-028) and resolves at read time (D-031, D-035). A large ratio is
# expected; a `unique` count that stops growing is not.
print(f"{'dataset':<16} {'stored':>8} {'unique':>8}")
for dataset, (stored, unique) in telemetry.counts().items():
    print(f"{dataset:<16} {stored:>8,} {unique:>8,}")

runs = telemetry.load("job_runs")
commits = telemetry.load("delta_commits")

# How far the commit record can be trusted. Runs more recent than this are not
# judged, because their commits may simply not have been collected yet.
EVALUATE_BEFORE = telemetry.complete_until(commits)

print()
print(f"evaluating runs that ended before {EVALUATE_BEFORE:%Y-%m-%d %H:%M} UTC")


# ============================================================================
# CELL 3 — Do the expectations match reality?
# ============================================================================

from detection.rules.silent_zero_row import WriteExpectation

EXPECTATIONS = [
    WriteExpectation(
        pipeline="pl_ingest_ledger_daily",
        table="ledger_daily",
        workspace="h1-finance-prod",
        minimum_rows=1,
    ),
]

# Checked rather than assumed. A name that matches nothing produces no
# incidents and looks exactly like a healthy estate — which has happened twice,
# once from a wrong table name and once from backticks left in one (D-032).
known_pipelines = set(runs["item_name"].unique())
known_tables = set(commits["table_name"].unique())

for expectation in EXPECTATIONS:
    if expectation.pipeline not in known_pipelines:
        print(f"NO SUCH PIPELINE: {expectation.pipeline!r}")
        print(f"  telemetry has: {sorted(known_pipelines)}")
    elif expectation.table not in known_tables:
        print(f"NO SUCH TABLE: {expectation.table!r}")
        print(f"  telemetry has: {sorted(known_tables)}")
    else:
        n = (runs["item_name"] == expectation.pipeline).sum()
        print(f"OK  {expectation.pipeline} -> {expectation.table}  ({n} runs)")


# ============================================================================
# CELL 4 — Run detection
# ============================================================================

from detection.rules import silent_zero_row

incidents = silent_zero_row.detect(
    runs=runs,
    commits=commits,
    expectations=EXPECTATIONS,
    evaluate_before=EVALUATE_BEFORE,
)

print(f"{len(incidents)} incidents from silent_zero_row")
print(f"{len(gaps)} incidents from collection_gap")

# A repeat here would mean the same run was counted twice — the duplicate
# problem reaching detection despite deduplication.
print(f"unique incident ids: {len({i.incident_id for i in incidents})}")

# How much of the commit record is still unmeasured. These runs are skipped
# rather than judged, because unknown is not the same as zero (D-026), so a
# rising number quietly shrinks what detection can see.
unknown = commits["rows_added"].isna().sum()
print(f"commits with an unknown row count: {unknown} of {len(commits)}")


# ============================================================================
# CELL 5 — What the incidents say
# ============================================================================

import pandas as pd

if not incidents:
    print("no incidents — check Cell 2 said current and Cell 3 said OK")
else:
    frame = pd.DataFrame(
        [
            {
                "incident_id": i.incident_id,
                "run_started": i.measured["run_started"],
                "duration_s": i.measured["run_duration_seconds"],
                "rows_delivered": i.measured["rows_delivered"],
            }
            for i in incidents
        ]
    )

    frame["run_started"] = pd.to_datetime(frame["run_started"])

    # Grouped by day, because the fault windows are what matter rather than
    # the individual runs. Each injection ran for hours, so a window appears
    # as a block of consecutive silent runs.
    by_day = frame.groupby(frame["run_started"].dt.date).agg(
        silent_runs=("incident_id", "count"),
        first_run=("run_started", "min"),
        last_run=("run_started", "max"),
    )

    print(by_day.to_string())
    print()
    print(f"earliest {frame['run_started'].min()}")
    print(f"latest   {frame['run_started'].max()}")


# ============================================================================
# APPENDIX A — The denominators in docs/EVALUATION.md
# ============================================================================
#
# Not part of the hourly run. Kept because every detection figure published in
# EVALUATION.md comes from this cell rather than from arithmetic done by hand:
# "roughly 170 runs" is not a benchmark result.
#
# Needs Cells 1-4 to have run, so `runs`, `commits`, `incidents` and
# `EVALUATE_BEFORE` are in the session.
#
# NOTE ON READING IT LATER. The numbers grow with the observation window. The
# figures in EVALUATION.md are for the window stated there, which ends before
# the 15 October controlled injection. Run today, this cell reports a larger
# `runs judged` and counts the 15 October incidents in `after restore` — those
# are a real injected fault, not false positives, and the published window
# excludes them deliberately so the quiet period means what it says.

import pandas as pd

PIPELINE = "pl_ingest_ledger_daily"
TABLE = "ledger_daily"

# The restore that ended the seeded fault episodes. Last silent run was 14:46
# on 7 October, so everything from the next run onward is the clean period.
RESTORED_AT = pd.Timestamp("2026-10-07 15:00:00+00:00")

# Only runs the rule actually judged: this pipeline, completed, finished early
# enough that their commits were known to have been collected.
judged = runs[
    (runs["item_name"] == PIPELINE)
    & (runs["status"] == "Completed")
    & runs["start_time"].notna()
    & runs["end_time"].notna()
    & (runs["end_time"] < EVALUATE_BEFORE)
]

flagged = {i.measured["run_id"] for i in incidents}

print(f"window            {judged['start_time'].min()}")
print(f"       to         {judged['end_time'].max()}")
print(f"runs judged       {len(judged)}")
print(f"runs flagged      {len(flagged)}")
print(f"runs not flagged  {len(judged) - len(flagged)}")
print()

# The two halves that matter. Before the restore the estate was faulty for
# long stretches; after it, healthy throughout. A false positive in the second
# half would be visible here and nowhere else.
before = judged[judged["start_time"] < RESTORED_AT]
after = judged[judged["start_time"] >= RESTORED_AT]

for label, subset in (("before restore", before), ("after restore", after)):
    hit = sum(1 for run_id in subset["run_id"] if run_id in flagged)
    print(f"{label:<16} {len(subset):>4} runs, {hit:>3} flagged")

print()

# The commit side, which is what the flags were derived from. The one-row gap
# between "delivered none" and "runs flagged" is the open discrepancy recorded
# in EVALUATION.md, and it has not been explained.
mine = commits[commits["table_name"] == TABLE]
rows = pd.to_numeric(mine["rows_added"], errors="coerce")

print(f"{TABLE} commits    {len(mine)}")
print(f"  delivered rows   {(rows > 0).sum()}")
print(f"  delivered none   {(rows == 0).sum()}")
print(f"  count unknown    {rows.isna().sum()}")
print(f"  typical delivery {int(rows[rows > 0].median()):,} rows")


# ============================================================================
# APPENDIX B — Which collections wrote which dataset
# ============================================================================
#
# Diagnostic, for when one dataset looks frozen and the others do not. If
# job_runs carries collection timestamps that delta_commits does not, those
# runs died partway through 10_collect — which is how the Delta schema
# collision was found (D-034).

print("job_runs      ", sorted(telemetry.load("job_runs")["collected_at"].unique())[-5:])
print("delta_commits ", sorted(telemetry.load("delta_commits")["collected_at"].unique())[-5:])
