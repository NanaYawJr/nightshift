# Fabric notebook: 01_seed_estate
#
# Generates the synthetic estate and writes it as Delta tables into
# lh_finance_bronze.
#
# The generator itself is NOT duplicated here. It is fetched from the repo at
# runtime so there is exactly one copy of that code, unit-tested locally (see
# tests/test_seed.py) and version-controlled. This notebook is only the
# execution wrapper.
#
# Attach this notebook to lh_finance_bronze in hl-finance-prod before running.
#
# Paste each block below into its own notebook cell, in order.


# ============================================================================
# CELL 1 — Fetch the generator from the repo
# ============================================================================

import urllib.request
from pathlib import Path

REPO = "NanaYawJr/nightshift"
BRANCH = "main"
MODULE = "estate/seed/generate.py"

url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{MODULE}"

# Write it beside the notebook so a plain import picks it up.
local = Path("generate.py")
urllib.request.urlretrieve(url, local)

print(f"Fetched {MODULE} ({local.stat().st_size:,} bytes)")
print(f"From    {url}")


# ============================================================================
# CELL 2 — Generate the data
# ============================================================================

import generate

ROWS = 2_000_000
SEED = 4417
START = "2025-03-01"
END = "2026-09-13"

tables = generate.generate_all(n_rows=ROWS, seed=SEED, start=START, end=END)

for name, frame in tables.items():
    print(f"{name:<12} {len(frame):>10,} rows   {len(frame.columns):>2} columns")


# ============================================================================
# CELL 3 — Write as Delta tables
# ============================================================================

# Arrow makes the pandas -> Spark conversion dramatically faster. Without it,
# 2 million rows converts row by row through Python objects.
spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")

for name, frame in tables.items():
    sdf = spark.createDataFrame(frame)

    (
        sdf.write
        .mode("overwrite")
        .format("delta")
        .saveAsTable(name)
    )

    print(f"wrote {name}")

print("\nAll tables written.")


# ============================================================================
# CELL 4 — Verify
# ============================================================================

# Row counts straight from the Delta tables, not from the pandas frames.
# This confirms what actually landed rather than what was generated.
for name in tables:
    count = spark.table(name).count()
    print(f"{name:<12} {count:>10,}")

print()
display(spark.table("Ledger").limit(10))


# ============================================================================
# CELL 5 — Confirm the transaction log is readable
# ============================================================================

# `collectors/delta_commits` will read row counts from this log to detect
# pipelines that succeed while delivering nothing. Worth proving now that the
# history is there and has the fields the collector needs.

history = spark.sql("DESCRIBE HISTORY Ledger")
display(
    history.select(
        "version", "timestamp", "operation", "operationMetrics"
    ).limit(5)
)


# ============================================================================
# CELL 6 — Cardinality check against the local tests
# ============================================================================

# tests/test_seed.py asserts this distribution on small samples. This confirms
# it survived at full scale and through the Delta write.

from pyspark.sql import functions as F

ledger = spark.table("Ledger")

counts = ledger.select(
    [F.countDistinct(c).alias(c) for c in ledger.columns]
).collect()[0].asDict()

for column, distinct in sorted(counts.items(), key=lambda kv: -kv[1]):
    share = distinct / ledger.count()
    print(f"{column:<20} {distinct:>10,}   {share:>7.2%} of rows")