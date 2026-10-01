"""Generate the daily landing file the ingest pipeline consumes.

Run from a Fabric notebook with lh_finance_bronze attached:

    import landing
    landing.write_batch(rows=5000, batch_date="2026-10-01")

The estate needs a source that is separate from its destination, or there is no
pipeline to build. This writes a CSV into the Lakehouse's Files area; the
pipeline copies it into a Delta table. That separation is what makes
`silent_zero_row` possible: the file's schema can be changed without touching
the destination, which is exactly how the fault arrives in real estates.

CSV rather than Parquet deliberately. A CSV carries no schema of its own, so a
copy activity has to be told what the columns are — and a mapping that has been
told something which is no longer true is the whole mechanism of the fault.

Column names and order match the `ledger` table exactly. A batch is a day's
transactions, not a reload.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# Mirrors estate/seed/generate.py. Duplicated rather than imported because this
# module runs in a notebook where only this file is fetched.
BRANCHES = [
    "Kumasi Central", "Accra North", "Takoradi", "Tamale", "Cape Coast",
    "Ho", "Sunyani", "Koforidua", "Wa", "Bolgatanga", "Techiman", "Obuasi",
]
BRANCH_WEIGHTS = np.array(
    [0.22, 0.19, 0.14, 0.09, 0.07, 0.06, 0.05, 0.05, 0.04, 0.04, 0.03, 0.02]
)
PRODUCTS = [
    "MICRO-01", "MICRO-02", "SME-01", "SME-02",
    "AGRI-01", "AGRI-02", "GROUP-01", "EMERG-01",
]
CHANNELS = ["Branch", "Mobile", "Agent", "USSD", "Web"]
STATUSES = ["Posted", "Pending", "Reversed", "Held"]
CURRENCIES = ["GHS", "USD", "GBP"]
OFFICERS = [f"officer_{i:03d}" for i in range(30)]

N_CLIENTS = 45_000

# Batches start well above the seeded range so IDs never collide.
ID_OFFSET = 9_000_000

LANDING_DIR = "/lakehouse/default/Files/landing"


def generate_batch(
    rows: int = 5_000,
    batch_date: str = "2026-10-01",
    seed: int | None = None,
) -> pd.DataFrame:
    """One day's transactions, in the same shape as the ledger table.

    Seeded on the batch date by default, so re-running a given day reproduces
    that day exactly — the benchmark depends on being able to replay.
    """
    if seed is None:
        seed = int(batch_date.replace("-", ""))

    rng = np.random.default_rng(seed)
    day = pd.Timestamp(batch_date)

    seconds = np.clip(
        rng.normal(loc=11 * 3600, scale=2.5 * 3600, size=rows), 0, 86399
    ).astype(np.int64)

    first_id = ID_OFFSET + seed % 1_000_000

    return pd.DataFrame(
        {
            "TransactionID": [
                f"TXN-{i:010d}" for i in range(first_id, first_id + rows)
            ],
            "ClientID": rng.integers(1, N_CLIENTS + 1, size=rows),
            "Branch": rng.choice(BRANCHES, size=rows, p=BRANCH_WEIGHTS),
            "ProductCode": rng.choice(PRODUCTS, size=rows),
            "Channel": rng.choice(
                CHANNELS, size=rows, p=[0.31, 0.34, 0.19, 0.11, 0.05]
            ),
            "TransactionDate": day.strftime("%Y-%m-%d"),
            "PostedTimestamp": [
                (day + pd.Timedelta(seconds=int(s))).strftime("%Y-%m-%d %H:%M:%S")
                for s in seconds
            ],
            "Amount": np.round(rng.lognormal(mean=6.2, sigma=1.1, size=rows), 2),
            "CurrencyCode": rng.choice(CURRENCIES, size=rows, p=[0.94, 0.05, 0.01]),
            "Status": rng.choice(STATUSES, size=rows, p=[0.91, 0.05, 0.03, 0.01]),
            "SourceRef": [f"SRC-{r:09d}" for r in rng.integers(1, 500_000, size=rows)],
            "PostedBy": rng.choice(OFFICERS, size=rows),
        }
    )


def write_batch(
    rows: int = 5_000,
    batch_date: str = "2026-10-01",
    rename: dict[str, str] | None = None,
    directory: str = LANDING_DIR,
) -> str:
    """Write one batch as a single CSV into the landing area.

    `rename` exists for the schema-drift fault: passing
    {"PostedTimestamp": "posted_ts"} writes an otherwise identical file whose
    column header no longer matches what the pipeline's mapping expects.

    Written with pandas rather than Spark so the result is one file rather than
    a folder of part files. A copy activity can point at a folder, but a single
    predictable filename is easier to reason about and easier to overwrite.
    """
    frame = generate_batch(rows=rows, batch_date=batch_date)

    if rename:
        frame = frame.rename(columns=rename)

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    path = target / "transactions_current.csv"
    frame.to_csv(path, index=False)

    print(f"wrote {len(frame):,} rows to {path}")
    print(f"columns: {list(frame.columns)}")

    return str(path)