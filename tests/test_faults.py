"""Fault framework tests.

Spark is faked. These test the contract — ground truth, reversibility,
determinism, reconstruction — and which operations a fault asks for, not whether
they execute. Only Fabric can confirm Spark execution.

Reconstruction tests build a GroundTruth directly from a fault's parameters
rather than injecting first. That is both faster and a truer test: rebuilding is
supposed to need nothing but the sealed record.

SilentZeroRow is the exception to the faked-Spark rule. It touches only the
filesystem, so it is tested against real temporary files and its behaviour here
is its behaviour in Fabric.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from estate.faults.base import Fault, GroundTruth, Seal
from estate.faults.library import (
    IMPLEMENTED,
    PENDING,
    ColumnWidening,
    SchemaDrift,
    SilentZeroRow,
    narrow_expression,
    rebuild,
    widen_expression,
)

HEADER = "TransactionID,ClientID,Branch,Amount"
ROWS = [
    "TXN-0000000001,42,Kumasi Central,120.50",
    "TXN-0000000002,99,Accra North,89.00",
    "TXN-0000000003,17,Takoradi,2400.75",
]


def seal_record(fault: Fault, workspace: str = "ws", item: str = "ledger") -> GroundTruth:
    """Build the sealed record a fault would produce, without injecting it.

    Lets reconstruction be tested for faults whose `_apply` needs PySpark, which
    is not installed locally.
    """
    return GroundTruth(
        fault_id=fault.fault_id,
        fault_class=fault.fault_class,
        target_workspace=workspace,
        target_item=item,
        mechanism=fault.mechanism(),
        expected_symptom=fault.expected_symptom(),
        injected_at=datetime.now(),
        seed=fault.seed,
        parameters=fault.parameters(),
    )


@pytest.fixture
def landing(tmp_path):
    """A landing file with a header and three data rows."""
    path = tmp_path / "landing" / "transactions_current.csv"
    path.parent.mkdir(parents=True)
    path.write_text(HEADER + "\n" + "\n".join(ROWS) + "\n")
    return path


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


class FakeFrame:
    def __init__(self, log: list):
        self.log = log

    def withColumnRenamed(self, old, new):
        self.log.append(("rename", old, new))
        return self

    def withColumn(self, name, expr):
        self.log.append(("withColumn", name))
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

    @classmethod
    def _rebuild(cls, truth):
        fault = cls(truth.target_workspace, truth.target_item, seed=truth.seed)
        fault._revert_state = {"applied": True}
        return fault

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


def test_inject_and_seal_records_immediately(tmp_path):
    """A fault injected but not sealed cannot be reverted by anything but the
    live object that made it."""
    seal = Seal(tmp_path)
    fault = TrivialFault("ws", "item")

    truth = fault.inject_and_seal(FakeSpark(), seal)

    assert seal.list_ids() == [truth.fault_id]


# --- Identity --------------------------------------------------------------


def test_fault_id_is_stable_across_instances():
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
    a = SchemaDrift("ws", "ledger", column="PostedTimestamp", new_name="posted_ts")
    b = SchemaDrift("ws", "ledger", column="SourceRef", new_name="source_ref")
    assert a.fault_id != b.fault_id


# --- Reconstruction from sealed ground truth -------------------------------


def test_rebuilt_fault_reverts_without_the_original_object():
    """The point of D-022. Four notebook sessions died mid-fault during
    development, each time stranding the estate."""
    original = SchemaDrift("h1-finance-prod", "ledger")
    truth = original.inject(FakeSpark())

    del original

    spark = FakeSpark()
    SchemaDrift.from_ground_truth(truth).revert(spark)

    assert ("rename", "posted_ts", "PostedTimestamp") in spark.log


def test_rebuilt_fault_keeps_its_identity():
    """Rebuilt from the sealed record alone — no injection, so no Spark needed."""
    original = ColumnWidening("ws", "ledger", column="PostedBy", separator="|")
    truth = seal_record(original)

    rebuilt = ColumnWidening.from_ground_truth(truth)

    assert rebuilt.fault_id == truth.fault_id
    assert rebuilt.column == "PostedBy"
    assert rebuilt.separator == "|"
    assert rebuilt._revert_state == {"column": "PostedBy", "separator": "|"}


def test_rebuilding_the_wrong_class_is_refused():
    truth = seal_record(SchemaDrift("ws", "ledger"))

    with pytest.raises(ValueError, match="cannot rebuild"):
        ColumnWidening.from_ground_truth(truth)


def test_rebuild_dispatches_on_fault_class():
    """Cleanup should not need to know which class injected a fault."""
    truth = seal_record(ColumnWidening("ws", "ledger"))

    assert isinstance(rebuild(truth), ColumnWidening)


def test_rebuild_rejects_an_unknown_class():
    truth = GroundTruth(
        fault_id="x",
        fault_class="not_a_real_class",
        target_workspace="ws",
        target_item="ledger",
        mechanism="",
        expected_symptom="",
        injected_at=datetime.now(),
        seed=1,
    )
    with pytest.raises(KeyError, match="No implementation"):
        rebuild(truth)


def test_every_implemented_fault_can_be_rebuilt():
    """A fault whose parameters cannot rebuild it is under-recorded.

    Covers Spark-dependent faults too, since nothing is injected.
    """
    for cls in IMPLEMENTED.values():
        truth = seal_record(cls("ws", "target"), item="target")
        rebuilt = cls.from_ground_truth(truth)

        assert rebuilt._revert_state, f"{cls.__name__} rebuilt with empty revert state"
        assert rebuilt.fault_id == truth.fault_id


def test_round_trip_through_the_seal_reverts_a_real_file(landing, tmp_path):
    """End to end, with no live object and real filesystem effects.

    Mirrors the recovery done by hand after a session timeout: load the sealed
    record, rebuild, revert.
    """
    seal = Seal(tmp_path / "sealed")

    fault = SilentZeroRow(
        "h1-finance-prod",
        "pl_ingest_ledger_daily",
        landing_path=str(landing),
        backup_dir=str(tmp_path / "backup"),
    )
    original = landing.read_text()
    truth = fault.inject_and_seal(FakeSpark(), seal)

    del fault

    reloaded = seal.load(truth.fault_id)
    rebuild(reloaded).revert(FakeSpark())

    assert landing.read_text() == original


# --- Schema drift ----------------------------------------------------------


def test_schema_drift_renames_by_rewrite():
    fault = SchemaDrift("h1-finance-prod", "ledger")
    spark = FakeSpark()

    fault.inject(spark)

    assert ("rename", "PostedTimestamp", "posted_ts") in spark.log
    assert ("option", "overwriteSchema", "true") in spark.log
    assert ("saveAsTable", "ledger") in spark.log


def test_schema_drift_never_alters_the_table_in_place():
    """ALTER TABLE RENAME COLUMN requires Delta column mapping, a one-way
    protocol upgrade. The rewrite approach must never issue it."""
    fault = SchemaDrift("h1-finance-prod", "ledger")
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)

    assert not any("ALTER" in s for s in spark.statements)


def test_schema_drift_revert_restores_the_original_name():
    fault = SchemaDrift("h1-finance-prod", "ledger")
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)

    renames = [entry for entry in spark.log if entry[0] == "rename"]
    assert renames == [
        ("rename", "PostedTimestamp", "posted_ts"),
        ("rename", "posted_ts", "PostedTimestamp"),
    ]


def test_schema_drift_ground_truth_names_both_columns():
    truth = SchemaDrift("h1-finance-prod", "ledger").inject(FakeSpark())

    assert truth.fault_class == "schema_drift"
    assert truth.parameters["original_column"] == "PostedTimestamp"
    assert truth.parameters["renamed_to"] == "posted_ts"


# --- Column widening -------------------------------------------------------


def test_widen_expression_appends_the_uniquifier():
    assert (
        widen_expression("SourceRef", "TransactionID", "#")
        == "concat(SourceRef, '#', TransactionID)"
    )


def test_narrow_expression_takes_everything_before_the_separator():
    assert narrow_expression("SourceRef", "#") == "substring_index(SourceRef, '#', 1)"


def test_the_two_expressions_use_the_same_separator():
    """A mismatch here would make revert silently leave the data widened."""
    assert "'|'" in widen_expression("SourceRef", "TransactionID", "|")
    assert "'|'" in narrow_expression("SourceRef", "|")


def test_column_widening_declares_no_schema_change():
    """The defining property. A schema diff must see nothing."""
    assert ColumnWidening("ws", "ledger").parameters()["schema_changed"] is False


def test_column_widening_symptom_says_schema_looks_unchanged():
    assert "schema comparison shows" in ColumnWidening("ws", "ledger").expected_symptom()


def test_column_widening_mechanism_names_the_column_and_uniquifier():
    mechanism = ColumnWidening("ws", "ledger").mechanism()
    assert "SourceRef" in mechanism
    assert "TransactionID" in mechanism


def test_column_widening_is_registered_as_model_bloat():
    """The taxonomy is unchanged: the mechanism changed, the fault class did not."""
    assert ColumnWidening.fault_class == "model_bloat"


# --- Silent zero row -------------------------------------------------------


def test_silent_zero_row_leaves_only_the_header(landing, tmp_path):
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    fault.inject(FakeSpark())

    assert landing.read_text().splitlines() == [HEADER]


def test_silent_zero_row_keeps_the_file_readable(landing, tmp_path):
    """A corrupt file would fail the copy loudly. The point is that it does not."""
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    fault.inject(FakeSpark())

    content = landing.read_text()
    assert content.endswith("\n")
    assert content.count(",") == HEADER.count(",")


def test_silent_zero_row_backs_up_before_truncating(landing, tmp_path):
    backup_dir = tmp_path / "backup"
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(backup_dir)
    )
    fault.inject(FakeSpark())

    backup = backup_dir / "transactions_current.csv"
    assert backup.exists()
    assert len(backup.read_text().splitlines()) == 4


def test_silent_zero_row_backup_sits_outside_the_landing_folder(landing, tmp_path):
    """A pipeline reading the folder rather than a named file must not ingest
    the backup."""
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    fault.inject(FakeSpark())

    assert list(landing.parent.glob("*.csv")) == [landing]


def test_silent_zero_row_revert_restores_every_row(landing, tmp_path):
    original = landing.read_text()

    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    fault.inject(FakeSpark())
    fault.revert(FakeSpark())

    assert landing.read_text() == original


def test_silent_zero_row_revert_removes_the_backup(landing, tmp_path):
    """A stale backup would be restored over a later, different file."""
    backup_dir = tmp_path / "backup"
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(backup_dir)
    )
    fault.inject(FakeSpark())
    fault.revert(FakeSpark())

    assert not (backup_dir / "transactions_current.csv").exists()


def test_silent_zero_row_can_be_injected_twice(landing, tmp_path):
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    spark = FakeSpark()

    fault.inject(spark)
    fault.revert(spark)
    fault.inject(spark)

    assert landing.read_text().splitlines() == [HEADER]


def test_silent_zero_row_refuses_when_there_is_no_landing_file(tmp_path):
    fault = SilentZeroRow(
        "ws",
        "pl",
        landing_path=str(tmp_path / "missing.csv"),
        backup_dir=str(tmp_path / "backup"),
    )
    with pytest.raises(FileNotFoundError, match="No landing file"):
        fault.inject(FakeSpark())


def test_silent_zero_row_revert_refuses_without_a_backup(landing, tmp_path):
    backup_dir = tmp_path / "backup"
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(backup_dir)
    )
    fault.inject(FakeSpark())

    (backup_dir / "transactions_current.csv").unlink()

    with pytest.raises(FileNotFoundError, match="Backup missing"):
        fault.revert(FakeSpark())


def test_silent_zero_row_symptom_names_the_only_evidence(landing):
    """Row count is the sole signal. Status and duration both look normal."""
    symptom = SilentZeroRow("ws", "pl", landing_path=str(landing)).expected_symptom()

    assert "numOutputRows 0" in symptom
    assert "Succeeded" in symptom


def test_silent_zero_row_parameters_carry_both_paths(landing, tmp_path):
    """D-022: revert must be reconstructible from sealed ground truth alone."""
    fault = SilentZeroRow(
        "ws", "pl", landing_path=str(landing), backup_dir=str(tmp_path / "backup")
    )
    params = fault.parameters()

    assert params["landing_path"] == str(landing)
    assert params["backup_path"].endswith("transactions_current.csv")


# --- Seal ------------------------------------------------------------------


def test_seal_round_trips(tmp_path):
    seal = Seal(tmp_path)
    truth = SchemaDrift("h1-finance-prod", "ledger").inject(FakeSpark())

    seal.store(truth)
    loaded = seal.load(truth.fault_id)

    assert loaded.fault_id == truth.fault_id
    assert loaded.mechanism == truth.mechanism
    assert isinstance(loaded.injected_at, datetime)


def test_seal_lists_what_it_holds(tmp_path):
    seal = Seal(tmp_path)

    for column in ("PostedTimestamp", "SourceRef"):
        fault = SchemaDrift("ws", "ledger", column=column, new_name=column.lower())
        seal.store(fault.inject(FakeSpark()))

    assert len(seal.list_ids()) == 2


def test_seal_refuses_to_overwrite(tmp_path):
    """Silent clobbering would corrupt a benchmark with no error."""
    seal = Seal(tmp_path)
    seal.store(SchemaDrift("ws", "ledger").inject(FakeSpark()))

    with pytest.raises(FileExistsError, match="already sealed"):
        seal.store(SchemaDrift("ws", "ledger").inject(FakeSpark()))


def test_seal_discard_removes_a_record(tmp_path):
    """A stale seal would let a later revert restore a backup that no longer
    matches the estate."""
    seal = Seal(tmp_path)
    truth = SchemaDrift("ws", "ledger").inject(FakeSpark())
    seal.store(truth)

    seal.discard(truth.fault_id)

    assert seal.list_ids() == []


def test_sealed_file_is_valid_json(tmp_path):
    seal = Seal(tmp_path)
    path = seal.store(SchemaDrift("ws", "ledger").inject(FakeSpark()))

    payload = json.loads(path.read_text())
    assert payload["fault_class"] == "schema_drift"
    assert "mechanism" in payload


# --- Registry --------------------------------------------------------------


def test_registry_matches_what_exists():
    assert set(IMPLEMENTED) == {"schema_drift", "model_bloat", "silent_zero_row"}


def test_pending_classes_are_not_silently_missing():
    assert len(PENDING) == 3
    assert "refresh_collision" in PENDING


def test_every_implemented_fault_declares_its_class():
    for name, cls in IMPLEMENTED.items():
        assert cls.fault_class == name
        assert cls.fault_class != Fault.fault_class