"""Fault framework tests.

Spark is faked. These test the contract — ground truth, reversibility,
determinism — and which operations a fault asks for, not whether they execute.
Only Fabric can confirm execution.
"""

import json
from datetime import datetime

import pytest

from estate.faults.base import Fault, Seal
from estate.faults.library import IMPLEMENTED, PENDING, ModelBloat, SchemaDrift


class FakeWriter:
    """Records the write chain: mode, options, format, target table."""

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


class FakeFrame:
    """Records transformations applied to a table read."""

    def __init__(self, log: list):
        self.log = log

    def withColumnRenamed(self, old, new):
        self.log.append(("rename", old, new))
        return self

    def drop(self, column):
        self.log.append(("drop", column))
        return self

    @property
    def write(self):
        return FakeWriter(self.log)


class FakeSpark:
    """Records what was asked of it. Executes nothing."""

    def __init__(self):
        self.statements: list[str] = []
        self.log: list[tuple] = []

    def sql(self, statement: str):
        self.statements.append(" ".join(statement.split()))
        return None

    def table(self, name: str):
        self.log.append(("table", name))
        return FakeFrame(self.log)


class TrivialFault(Fault):
    """Minimal implementation, for testing the base class itself."""

    fault_class = "test_fault"

    def mechanism(self) -> str:
        return "nothing was changed"

    def expected_symptom(self) -> str:
        return "nothing should be noticed"

    def parameters(self) -> dict:
        return {"noop": True}

    def _apply(self, spark) -> None:
        self._revert_state = {"applied": True}

    def _undo(self, spark) -> None:
        pass


# --- Contract --------------------------------------------------------------


def test_ground_truth_requires_injection():
    fault = TrivialFault("ws", "item")
    with pytest.raises(RuntimeError, match="only meaningful once injected"):
        fault.ground_truth()


def test_double_injection_is_refused():
    fault = TrivialFault("ws", "item")
    fault.inject(FakeSpark())
    with pytest.raises(RuntimeError, match="already injected"):
        fault.inject(FakeSpark())


def test_revert_before_inject_is_refused():
    fault = TrivialFault("ws", "item")
    with pytest.raises(RuntimeError, match="never injected"):
        fault.revert(FakeSpark())


def test_revert_allows_reinjection():
    """A benchmark that can only run once is not a benchmark."""
    fault = TrivialFault("ws", "item")
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)
    fault.inject(spark)  # must not raise


# --- Identity --------------------------------------------------------------


def test_fault_id_is_stable_across_instances():
    """Same seed, same target, same parameters — same id."""
    a = TrivialFault("ws", "item", seed=99)
    b = TrivialFault("ws", "item", seed=99)
    assert a.fault_id == b.fault_id


def test_seed_changes_the_id():
    a = TrivialFault("ws", "item", seed=1)
    b = TrivialFault("ws", "item", seed=2)
    assert a.fault_id != b.fault_id


def test_parameters_change_the_id():
    """Regression: two schema drifts on one table collided and the second
    silently overwrote the first's sealed ground truth."""
    a = SchemaDrift("ws", "Ledger", column="PostedTimestamp", new_name="posted_ts")
    b = SchemaDrift("ws", "Ledger", column="SourceRef", new_name="source_ref")
    assert a.fault_id != b.fault_id


# --- Schema drift ----------------------------------------------------------


def test_schema_drift_renames_by_rewrite():
    fault = SchemaDrift("hl-finance-prod", "Ledger")
    spark = FakeSpark()

    fault.inject(spark)

    assert ("rename", "PostedTimestamp", "posted_ts") in spark.log
    assert ("option", "overwriteSchema", "true") in spark.log
    assert ("saveAsTable", "Ledger") in spark.log


def test_schema_drift_never_alters_the_table_in_place():
    """Regression: ALTER TABLE RENAME COLUMN requires Delta column mapping,
    a one-way protocol upgrade. The rewrite approach must never issue it."""
    fault = SchemaDrift("hl-finance-prod", "Ledger")
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)

    assert not any("ALTER" in s for s in spark.statements)


def test_schema_drift_revert_restores_the_original_name():
    fault = SchemaDrift("hl-finance-prod", "Ledger")
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)

    renames = [entry for entry in spark.log if entry[0] == "rename"]
    assert renames == [
        ("rename", "PostedTimestamp", "posted_ts"),
        ("rename", "posted_ts", "PostedTimestamp"),
    ]


def test_schema_drift_ground_truth_names_both_columns():
    fault = SchemaDrift("hl-finance-prod", "Ledger")
    truth = fault.inject(FakeSpark())

    assert truth.fault_class == "schema_drift"
    assert truth.parameters["original_column"] == "PostedTimestamp"
    assert truth.parameters["renamed_to"] == "posted_ts"
    assert "PostedTimestamp" in truth.mechanism


def test_schema_drift_accepts_a_different_column():
    fault = SchemaDrift("ws", "Ledger", column="SourceRef", new_name="source_ref")
    spark = FakeSpark()
    fault.inject(spark)
    assert ("rename", "SourceRef", "source_ref") in spark.log


# --- Model bloat -----------------------------------------------------------


def test_model_bloat_ground_truth_flags_no_consumer():
    """The diagnosis hinges on the column being unused. Say so explicitly.

    `_apply` needs real PySpark functions, so only the declaration is checked.
    """
    fault = ModelBloat("hl-finance-prod", "Ledger")

    assert "no downstream consumer" in fault.mechanism()
    assert fault.parameters()["cardinality"] == "near-unique"


def test_model_bloat_symptom_is_a_step_not_a_drift():
    """Step changes and gradual drift need different detection. Be precise."""
    fault = ModelBloat("ws", "Ledger")
    assert "single step" in fault.expected_symptom()


# --- Seal ------------------------------------------------------------------


def test_seal_round_trips(tmp_path):
    seal = Seal(tmp_path)
    fault = SchemaDrift("hl-finance-prod", "Ledger")
    truth = fault.inject(FakeSpark())

    seal.store(truth)
    loaded = seal.load(truth.fault_id)

    assert loaded.fault_id == truth.fault_id
    assert loaded.mechanism == truth.mechanism
    assert isinstance(loaded.injected_at, datetime)


def test_seal_lists_what_it_holds(tmp_path):
    seal = Seal(tmp_path)

    for column in ("PostedTimestamp", "SourceRef"):
        fault = SchemaDrift("ws", "Ledger", column=column, new_name=column.lower())
        seal.store(fault.inject(FakeSpark()))

    assert len(seal.list_ids()) == 2


def test_seal_refuses_to_overwrite(tmp_path):
    """Silent clobbering would corrupt a benchmark with no error."""
    seal = Seal(tmp_path)

    first = SchemaDrift("ws", "Ledger")
    seal.store(first.inject(FakeSpark()))

    second = SchemaDrift("ws", "Ledger")
    with pytest.raises(FileExistsError, match="already sealed"):
        seal.store(second.inject(FakeSpark()))


def test_sealed_file_is_valid_json(tmp_path):
    """The harness reads these. Malformed output would fail silently at scoring."""
    seal = Seal(tmp_path)
    fault = SchemaDrift("ws", "Ledger")
    path = seal.store(fault.inject(FakeSpark()))

    payload = json.loads(path.read_text())
    assert payload["fault_class"] == "schema_drift"
    assert "mechanism" in payload


# --- Registry --------------------------------------------------------------


def test_registry_matches_what_exists():
    assert set(IMPLEMENTED) == {"schema_drift", "model_bloat"}


def test_pending_classes_are_not_silently_missing():
    """The gap is recorded in code, not only in the plan."""
    assert len(PENDING) == 4
    assert "silent_zero_row" in PENDING


def test_every_implemented_fault_declares_its_class():
    for name, cls in IMPLEMENTED.items():
        assert cls.fault_class == name
        assert cls.fault_class != Fault.fault_class