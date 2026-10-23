# Fabric notebook: 10_collect
#
# Writes telemetry about the estate into lh_nightshift. Runs hourly on a
# schedule; everything downstream reads what this notebook produced and
# nothing else.
#
# Collector code is NOT duplicated here. It is fetched from the repository at
# runtime so there is exactly one copy, unit-tested locally. This notebook is
# the execution wrapper and the configuration — which tables, which sources,
# how far back.
#
# WHERE IT LIVES
#   Workspace:   h1-nightshift
#   Lakehouse:   lh_nightshift, pinned as default
#   Environment: env_nightshift (REQUIRED — %pip is disabled in scheduled
#                runs, so dependencies come from the Environment, D-033)
#   Schedule:    hourly
#
# The estate it observes is in a different workspace (h1-finance-prod) and is
# only ever read.
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

work = Path("/tmp/nightshift-collect")
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
# will keep using it however many times this cell re-downloads the code.
#
# The package list is derived from what was just downloaded rather than
# written here by hand (D-048). The hand-written version omitted `eval`, so
# eval.harness survived every re-run of this cell and a notebook spent an
# afternoon scoring with code that had been replaced on main hours before. A
# list maintained by hand will be wrong again; one derived from the repo
# cannot be.
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

# Printed so the next time this silently fails, it doesn't.
print(f"repo at {package_root}")
print(f"packages: {sorted(OURS)}")
print(f"stale modules cleared: {len(stale)}")


# ============================================================================
# CELL 2 — Point storage at the Lakehouse
# ============================================================================

from common import auth, storage

# Telemetry goes to the default lakehouse (lh_nightshift) with a tel_ prefix,
# so it is never mistaken for the estate it observes.
storage.use_lakehouse(spark, prefix="tel_")

print(f"backend:   {storage.backend_name()}")
print(f"in fabric: {auth.in_fabric()}")

# Proves the notebook identity works before any collector runs. A failure here
# is an auth problem; a failure later is a collector problem. The notebook
# authenticates as itself — there is no service principal secret in Fabric
# (D-029) — and the token's length is printed rather than the token.
print(f"token:     {len(auth.get_token())} chars")


# ============================================================================
# CELL 3 — Collect from the REST APIs
# ============================================================================

from collectors import refresh_history, rest_jobs

# Each collector writes its own datasets through storage, so there is nothing
# to pass and nothing to save here. The counts are what it found — a collector
# that finds nothing writes nothing, by design.
jobs = rest_jobs.collect(since_days=30)
print(f"items {jobs['items']}, job_runs {jobs['job_runs']}")

refreshes = refresh_history.collect(since_days=90)
print(f"datasets {refreshes['datasets']}, refresh_runs {refreshes['refresh_runs']}")


# ============================================================================
# CELL 4 — Collect the Delta log
# ============================================================================

from collectors import delta_commits

# The estate is a schema-enabled Lakehouse in another workspace, so Spark needs
# the full four-part name: workspace, lakehouse, schema, table. Each part is
# backtick-quoted because the workspace name contains hyphens, which Spark's
# parser would otherwise read as subtraction. The unqualified
# lh_finance_bronze.dbo.ledger form resolves as a child of the current schema
# and silently finds nothing (D-030). This is the only place in the project
# that needs qualification; the D-018 rename lands here.
ESTATE = "`h1-finance-prod`.`lh_finance_bronze`.`dbo`"
TABLES = ["ledger", "ledger_daily", "repayments", "clients"]

rows = delta_commits.collect(
    spark,
    [f"{ESTATE}.`{t}`" for t in TABLES],
    workspace="h1-finance-prod",
    max_counted=200,
)


def short_name(qualified: str) -> str:
    """Last part of a qualified name, without its backticks.

    Detection's expectations are written against the table — `ledger_daily` —
    not against where it happens to be mounted. Leaving the quoting in would
    store `` `ledger_daily` ``, which matches no expectation and produces a
    clean run with zero incidents: a false negative that looks like health.
    """
    return qualified.split(".")[-1].strip("`")


for row in rows:
    row["table_name"] = short_name(row["table_name"])

storage.write("delta_commits", rows)

# Printed so a wrong name is obvious here rather than three cells later.
print(f"tables recorded: {sorted({r['table_name'] for r in rows})}")
print(f"complete until {delta_commits.complete_until(rows)}")


# ============================================================================
# CELL 5 — Sample the source files
# ============================================================================

import notebookutils

from collectors import landing_files

# What the pipeline is given to copy. Unlike every other collector this one is
# a sampler: a Delta log keeps its own history and the REST APIs serve thirty
# days, but a file on disk has only a present state. A snapshot missed is gone,
# so the collection schedule is part of the measurement.
#
# This is the collector that made diagnosis possible. Without it, six
# diagnoses of the same fault class reached no cause at all; with it, five
# named the mechanism (see docs/EVALUATION.md).
FILES = (
    "abfss://h1-finance-prod@onelake.dfs.fabric.microsoft.com"
    "/lh_finance_bronze.Lakehouse/Files"
)

SOURCES = {
    "ledger_daily_landing": f"{FILES}/landing/transactions_current.csv",
}

sources = landing_files.collect(
    spark, notebookutils.fs, SOURCES, workspace="h1-finance-prod"
)

storage.write("landing_files", sources)

for row in sources:
    print(
        f"{row['source_name']:<24} exists={row['exists']} "
        f"rows={row['data_rows']} bytes={row['size_bytes']} "
        f"modified={row['modified_at']}"
    )


# ============================================================================
# CELL 6 — Verify
# ============================================================================

for dataset in (
    "items",
    "job_runs",
    "datasets",
    "refresh_runs",
    "delta_commits",
    "landing_files",
):
    frame = storage.read(dataset)
    print(f"{dataset:<16} {len(frame):>6,} rows")

# The end of the chain: what the detection rule will actually read. Guarded,
# because an empty frame has no columns and asking it for one raises an
# AttributeError that hides the real problem, which is upstream in Cell 4.
commits = storage.read("delta_commits")

print()
if commits.empty or "table_name" not in commits.columns:
    print("no commits collected — Cell 4 found nothing, check the table names")
else:
    daily = commits[commits.table_name == "ledger_daily"]
    print(f"ledger_daily commits: {len(daily)}")

    # A spread of values means the row-counting fix (D-026) is intact. Nothing
    # but nulls means _count_at could not read the table at a version, and
    # detection would be blind again.
    print(daily["rows_added"].value_counts(dropna=False).to_string())
