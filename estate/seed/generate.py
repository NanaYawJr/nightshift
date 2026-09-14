"""Generate the synthetic estate's data.

Run:  python -m estate.seed.generate --rows 2000000 --out data/seed

Produces a small star schema modelled on a retail lending ledger. The shape
matters more than the domain: what is being reproduced is the *cardinality
distribution* of real transactional data — a handful of very high-cardinality
columns carrying most of the storage cost, and a long tail of low-cardinality
ones carrying almost none.

Naive generators produce uniform cardinality across columns, which makes the
variance engine's job artificially easy. See D-003.

Everything is seeded. `--seed 4417` reproduces a run exactly, which the
evaluation harness relies on.

Note the base Ledger schema deliberately omits `TransactionNarrative`. That
column is added later by the schema-drift fault injector, reproducing the
incident where an upstream change is pulled into a semantic model by a wildcard
import. Generating it here would remove the fault.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

# --- Dimension sizes -------------------------------------------------------
# Chosen to give a realistic spread: two columns in the hundreds of thousands,
# several in the tens, and a few in single digits.

N_CLIENTS = 45_000
BRANCHES = [
    "Kumasi Central", "Accra North", "Takoradi", "Tamale", "Cape Coast",
    "Ho", "Sunyani", "Koforidua", "Wa", "Bolgatanga", "Techiman", "Obuasi",
]
PRODUCTS = [
    "MICRO-01", "MICRO-02", "SME-01", "SME-02",
    "AGRI-01", "AGRI-02", "GROUP-01", "EMERG-01",
]
CHANNELS = ["Branch", "Mobile", "Agent", "USSD", "Web"]
STATUSES = ["Posted", "Pending", "Reversed", "Held"]
CURRENCIES = ["GHS", "USD", "GBP"]
OFFICERS = [f"officer_{i:03d}" for i in range(30)]

# Branch volume is deliberately uneven — three branches do most of the business.
BRANCH_WEIGHTS = np.array(
    [0.22, 0.19, 0.14, 0.09, 0.07, 0.06, 0.05, 0.05, 0.04, 0.04, 0.03, 0.02]
)


def _weekday_weights(dates: pd.DatetimeIndex) -> np.ndarray:
    """Weekdays carry most volume; Sunday is nearly dead.

    Without this, anomaly detection on daily counts sees a flat line and any
    real dip looks identical to a weekend.
    """
    by_day = {0: 1.0, 1: 1.05, 2: 1.05, 3: 1.0, 4: 1.15, 5: 0.45, 6: 0.08}
    weights = np.array([by_day[d] for d in dates.dayofweek])
    return weights / weights.sum()


def generate_clients(rng: np.random.Generator) -> pd.DataFrame:
    """Client dimension. One row per client, ~45k rows."""
    client_ids = np.arange(1, N_CLIENTS + 1)

    return pd.DataFrame(
        {
            "ClientID": client_ids,
            # Unique per row: this is the high-cardinality string in the dim.
            "ClientRef": [f"CL-{i:07d}" for i in client_ids],
            "Branch": rng.choice(BRANCHES, size=N_CLIENTS, p=BRANCH_WEIGHTS),
            "Segment": rng.choice(
                ["Individual", "Group", "Micro-enterprise", "SME"],
                size=N_CLIENTS,
                p=[0.55, 0.22, 0.15, 0.08],
            ),
            "RegistrationDate": pd.to_datetime("2021-01-01")
            + pd.to_timedelta(rng.integers(0, 1800, size=N_CLIENTS), unit="D"),
            "RiskGrade": rng.choice(
                list("ABCDE"), size=N_CLIENTS, p=[0.15, 0.30, 0.32, 0.18, 0.05]
            ),
        }
    )


def generate_branches() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Branch": BRANCHES,
            "Region": [
                "Ashanti", "Greater Accra", "Western", "Northern", "Central",
                "Volta", "Bono", "Eastern", "Upper West", "Upper East",
                "Bono East", "Ashanti",
            ],
            "OpenedYear": [
                2009, 2011, 2013, 2015, 2014, 2016,
                2017, 2012, 2019, 2019, 2020, 2018,
            ],
        }
    )


def generate_products() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ProductCode": PRODUCTS,
            "ProductName": [
                "Micro Loan Standard", "Micro Loan Plus",
                "SME Working Capital", "SME Asset Finance",
                "Agri Seasonal", "Agri Equipment",
                "Group Solidarity", "Emergency Bridge",
            ],
            "TermMonths": [6, 12, 24, 36, 9, 24, 6, 3],
        }
    )


def generate_ledger(
    rng: np.random.Generator, n_rows: int, start: str, end: str
) -> pd.DataFrame:
    """The fact table. This is where the cardinality distribution lives.

    Column cardinality by design:
      TransactionID   n_rows      unique — no compression possible
      SourceRef       ~n_rows/2   high, but repeats
      ClientID        45,000      medium
      PostedTimestamp ~second     high, and usually nobody's idea of a problem
      Branch          12          trivial
      everything else <30         trivial
    """
    dates = pd.date_range(start, end, freq="D")
    day_index = rng.choice(
        len(dates), size=n_rows, p=_weekday_weights(dates)
    )
    transaction_dates = dates[day_index]

    # Posting time within the working day, clustered around late morning.
    seconds_into_day = np.clip(
        rng.normal(loc=11 * 3600, scale=2.5 * 3600, size=n_rows), 0, 86399
    ).astype(np.int64)

    # Amounts are lognormal — many small loans, a few very large ones.
    amounts = np.round(rng.lognormal(mean=6.2, sigma=1.1, size=n_rows), 2)

    # SourceRef repeats roughly twice on average: a batch reference, not a key.
    source_pool = np.arange(1, max(2, n_rows // 2) + 1)

    return pd.DataFrame(
        {
            "TransactionID": [f"TXN-{i:010d}" for i in range(1, n_rows + 1)],
            "ClientID": rng.integers(1, N_CLIENTS + 1, size=n_rows),
            "Branch": rng.choice(BRANCHES, size=n_rows, p=BRANCH_WEIGHTS),
            "ProductCode": rng.choice(PRODUCTS, size=n_rows),
            "Channel": rng.choice(
                CHANNELS, size=n_rows, p=[0.31, 0.34, 0.19, 0.11, 0.05]
            ),
            "TransactionDate": transaction_dates,
            "PostedTimestamp": transaction_dates
            + pd.to_timedelta(seconds_into_day, unit="s"),
            "Amount": amounts,
            "CurrencyCode": rng.choice(CURRENCIES, size=n_rows, p=[0.94, 0.05, 0.01]),
            "Status": rng.choice(STATUSES, size=n_rows, p=[0.91, 0.05, 0.03, 0.01]),
            "SourceRef": [
                f"SRC-{r:09d}" for r in rng.choice(source_pool, size=n_rows)
            ],
            "PostedBy": rng.choice(OFFICERS, size=n_rows),
        }
    )


def generate_repayments(rng: np.random.Generator, ledger: pd.DataFrame) -> pd.DataFrame:
    """Repayments, at roughly a third the volume of the ledger.

    Exists so the estate has a second fact table with its own refresh cadence —
    refresh collisions need at least two things to collide.
    """
    n = len(ledger) // 3
    sample = ledger.sample(n=n, random_state=int(rng.integers(0, 2**31)))

    return pd.DataFrame(
        {
            "RepaymentID": [f"RPY-{i:010d}" for i in range(1, n + 1)],
            "TransactionID": sample["TransactionID"].to_numpy(),
            "ClientID": sample["ClientID"].to_numpy(),
            "RepaymentDate": sample["TransactionDate"].to_numpy()
            + pd.to_timedelta(rng.integers(28, 90, size=n), unit="D"),
            "AmountPaid": np.round(
                sample["Amount"].to_numpy() * rng.uniform(0.15, 0.4, size=n), 2
            ),
            "Method": rng.choice(["Cash", "Mobile Money", "Transfer"], size=n),
        }
    )


def generate_all(
    n_rows: int, seed: int, start: str, end: str
) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)

    ledger = generate_ledger(rng, n_rows, start, end)

    return {
        "Ledger": ledger,
        "Repayments": generate_repayments(rng, ledger),
        "Clients": generate_clients(rng),
        "Branches": generate_branches(),
        "Products": generate_products(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=2_000_000, help="Ledger rows.")
    parser.add_argument("--seed", type=int, default=4417, help="Reproducibility.")
    parser.add_argument("--start", default="2025-03-01")
    parser.add_argument("--end", default="2026-09-13")
    parser.add_argument("--out", default="data/seed")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tables = generate_all(args.rows, args.seed, args.start, args.end)

    for name, frame in tables.items():
        path = out / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        size_mb = path.stat().st_size / 1_048_576
        print(f"{name:<12} {len(frame):>10,} rows   {size_mb:>7.1f} MB")

    print(f"\nWritten to {out}/  (seed {args.seed})")


if __name__ == "__main__":
    main()