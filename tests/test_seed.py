"""Seed generator tests.

These check the properties detection depends on, not the values themselves.
A generator that produces plausible-looking data with the wrong *shape* would
make the variance engine look better than it is.
"""

import pandas as pd
import pytest

from estate.seed import generate

# Small runs — these tests care about shape, not volume.
ROWS = 20_000
SEED = 4417
START, END = "2026-01-01", "2026-06-30"


@pytest.fixture(scope="module")
def tables():
    return generate.generate_all(ROWS, SEED, START, END)


def test_same_seed_reproduces_exactly():
    """The evaluation harness replays faults with --seed. This must hold."""
    a = generate.generate_all(5_000, 99, START, END)["Ledger"]
    b = generate.generate_all(5_000, 99, START, END)["Ledger"]
    pd.testing.assert_frame_equal(a, b)


def test_different_seeds_differ():
    a = generate.generate_all(5_000, 1, START, END)["Ledger"]
    b = generate.generate_all(5_000, 2, START, END)["Ledger"]
    assert not a["ClientID"].equals(b["ClientID"])


def test_transaction_id_is_unique(tables):
    """The unique high-cardinality column. Cannot compress — that's the point."""
    ledger = tables["Ledger"]
    assert ledger["TransactionID"].nunique() == len(ledger)


def test_cardinality_spans_orders_of_magnitude(tables):
    """The property D-003 exists to protect.

    Real transactional data has a few enormous columns and many tiny ones.
    A uniform distribution would make variance decomposition trivially easy.
    """
    ledger = tables["Ledger"]
    cardinalities = {c: ledger[c].nunique() for c in ledger.columns}

    assert cardinalities["TransactionID"] == len(ledger)
    assert cardinalities["SourceRef"] > len(ledger) * 0.3
    assert cardinalities["Branch"] == 12
    assert cardinalities["CurrencyCode"] == 3

    # At least three orders of magnitude between the widest and narrowest.
    assert max(cardinalities.values()) / min(cardinalities.values()) > 1_000


def test_narrative_column_is_absent(tables):
    """Added by the schema-drift fault injector, not by the generator.

    Generating it here would remove the fault the mockup's INC-0412 depends on.
    """
    assert "TransactionNarrative" not in tables["Ledger"].columns


def test_weekend_volume_is_lower(tables):
    """Detection needs weekly seasonality, or a real dip looks like a Sunday."""
    ledger = tables["Ledger"]
    by_weekday = ledger.groupby(ledger["TransactionDate"].dt.dayofweek).size()

    weekday_mean = by_weekday.loc[0:4].mean()
    sunday = by_weekday.loc[6]

    assert sunday < weekday_mean * 0.3


def test_branch_volume_is_uneven(tables):
    """Uniform branches would make every slice look equally guilty."""
    share = tables["Ledger"]["Branch"].value_counts(normalize=True)
    assert share.iloc[0] > 0.15
    assert share.iloc[-1] < 0.05


def test_amounts_are_skewed(tables):
    """Lognormal: many small loans, few large. Mean well above median."""
    amounts = tables["Ledger"]["Amount"]
    assert amounts.mean() > amounts.median() * 1.3
    assert amounts.min() > 0


def test_dates_stay_within_range(tables):
    ledger = tables["Ledger"]
    assert ledger["TransactionDate"].min() >= pd.Timestamp(START)
    assert ledger["TransactionDate"].max() <= pd.Timestamp(END)


def test_posted_timestamp_follows_transaction_date(tables):
    ledger = tables["Ledger"]
    same_day = ledger["PostedTimestamp"].dt.date == ledger["TransactionDate"].dt.date
    assert same_day.all()


def test_repayments_reference_real_transactions(tables):
    """Referential integrity, so relationship-based detection has something real."""
    ledger_ids = set(tables["Ledger"]["TransactionID"])
    repayment_refs = set(tables["Repayments"]["TransactionID"])
    assert repayment_refs.issubset(ledger_ids)


def test_repayments_fall_after_their_transaction(tables):
    merged = tables["Repayments"].merge(
        tables["Ledger"][["TransactionID", "TransactionDate"]],
        on="TransactionID",
        how="left",
    )
    assert (merged["RepaymentDate"] > merged["TransactionDate"]).all()


def test_client_ids_resolve(tables):
    client_ids = set(tables["Clients"]["ClientID"])
    assert set(tables["Ledger"]["ClientID"]).issubset(client_ids)


def test_dimensions_have_no_duplicate_keys(tables):
    assert tables["Clients"]["ClientID"].is_unique
    assert tables["Branches"]["Branch"].is_unique
    assert tables["Products"]["ProductCode"].is_unique