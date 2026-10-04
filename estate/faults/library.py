"""Fault implementations.

Three of the six classes are implemented. The rest need estate components that
do not exist yet and are listed at the bottom so the gap is visible in code
rather than only in a planning document.

Delta faults rewrite tables rather than altering them in place. In-place column
renames require Delta column mapping, a one-way protocol upgrade that some
readers do not support. A rewrite needs no upgrade and is closer to how these
faults arrive in real estates: an upstream source sends a new full load in a
new shape.

Every fault implements `_rebuild`, so any of them can be reverted from its
sealed record alone. See D-022.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from estate.faults.base import Fault, GroundTruth

LANDING_PATH = "/lakehouse/default/Files/landing/transactions_current.csv"
BACKUP_DIR = "/lakehouse/default/Files/_fault_backup"


def _overwrite(frame: Any, table: str, schema_change: bool = True) -> None:
    """Replace a Delta table's contents in one commit.

    `overwriteSchema` is only needed when columns are added, removed or renamed.
    A fault that changes values but not structure passes False — which is itself
    the point of such a fault: nothing about the table's shape looks different.

    Delta's snapshot isolation is what makes it safe to read and overwrite the
    same table in one job.
    """
    writer = frame.write.mode("overwrite")

    if schema_change:
        writer = writer.option("overwriteSchema", "true")

    writer.format("delta").saveAsTable(table)


def widen_expression(column: str, uniquifier: str, separator: str) -> str:
    """SQL for making `column` near-unique by appending `uniquifier`.

    Kept as a plain string so the logic is unit-testable without a Spark runtime.
    The separator must not occur in the original values, or the transform cannot
    be reversed — `_apply` checks this before writing.
    """
    return f"concat({column}, '{separator}', {uniquifier})"


def narrow_expression(column: str, separator: str) -> str:
    """SQL for undoing `widen_expression`: everything before the first separator."""
    return f"substring_index({column}, '{separator}', 1)"


class SchemaDrift(Fault):
    """An upstream source renames a column without telling anyone.

    Everything downstream that expects the old name either fails loudly, or —
    far worse — is configured to skip what it cannot find and quietly delivers
    nothing.

    Implemented as a rewrite, not ALTER TABLE, so the table's Delta protocol is
    never upgraded. The transaction log therefore records this as an overwrite
    with a changed schema, not as a RENAME COLUMN operation — detection must
    compare schemas between versions rather than look for a rename.
    """

    fault_class = "schema_drift"

    def __init__(
        self,
        target_workspace: str,
        target_item: str,
        column: str = "PostedTimestamp",
        new_name: str = "posted_ts",
        seed: int = 4417,
    ):
        super().__init__(target_workspace, target_item, seed)
        self.column = column
        self.new_name = new_name

    def mechanism(self) -> str:
        return (
            f"Column {self.column} in {self.target_item} was renamed to "
            f"{self.new_name} at the source. Nothing downstream was updated."
        )

    def expected_symptom(self) -> str:
        return (
            "Consumers referencing the old column name fail, return null, or "
            "silently drop rows depending on their error handling."
        )

    def parameters(self) -> dict[str, Any]:
        return {"original_column": self.column, "renamed_to": self.new_name}

    @classmethod
    def _rebuild(cls, truth: GroundTruth) -> "SchemaDrift":
        params = truth.parameters

        fault = cls(
            truth.target_workspace,
            truth.target_item,
            column=params["original_column"],
            new_name=params["renamed_to"],
            seed=truth.seed,
        )
        fault._revert_state = {
            "from": params["renamed_to"],
            "to": params["original_column"],
        }
        return fault

    def _apply(self, spark: Any) -> None:
        self._revert_state = {"from": self.new_name, "to": self.column}

        renamed = spark.table(self.target_item).withColumnRenamed(
            self.column, self.new_name
        )
        _overwrite(renamed, self.target_item, schema_change=True)

    def _undo(self, spark: Any) -> None:
        restored = spark.table(self.target_item).withColumnRenamed(
            self._revert_state["from"], self._revert_state["to"]
        )
        _overwrite(restored, self.target_item, schema_change=True)


class ColumnWidening(Fault):
    """A source starts putting near-unique values in a column that used to repeat.

    A column that repeats compresses well: the engine stores each distinct value
    once and points at it. When the source begins writing a unique value per row
    — appending a reference, a timestamp, a correlation id — the dictionary stops
    helping and the column's cost rises sharply.

    Nothing about the schema changes. The column count is identical, the types
    are identical, the name is identical. A schema diff sees nothing. Only
    cardinality moves, which is why diagnosing this requires comparing against
    history rather than inspecting the current state.

    This replaced an earlier implementation that added a high-cardinality column
    upstream and expected an Import model to pick it up. It does not: a Power BI
    Import model pins its column list at publish time and refresh re-reads only
    those columns. See D-019.
    """

    fault_class = "model_bloat"

    def __init__(
        self,
        target_workspace: str,
        target_item: str,
        column: str = "SourceRef",
        uniquifier: str = "TransactionID",
        separator: str = "#",
        seed: int = 4417,
    ):
        super().__init__(target_workspace, target_item, seed)
        self.column = column
        self.uniquifier = uniquifier
        self.separator = separator

    def mechanism(self) -> str:
        return (
            f"Values in {self.target_item}.{self.column} became near-unique — the "
            f"source began appending {self.uniquifier} to each value. The column "
            f"no longer compresses. No schema change accompanied it."
        )

    def expected_symptom(self) -> str:
        return (
            "Model size and refresh duration rise in a single step. Column count, "
            "row count and data types are unchanged, so a schema comparison shows "
            "nothing."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "widened_column": self.column,
            "uniquifier": self.uniquifier,
            "separator": self.separator,
            "schema_changed": False,
        }

    @classmethod
    def _rebuild(cls, truth: GroundTruth) -> "ColumnWidening":
        params = truth.parameters

        fault = cls(
            truth.target_workspace,
            truth.target_item,
            column=params["widened_column"],
            uniquifier=params["uniquifier"],
            separator=params["separator"],
            seed=truth.seed,
        )
        fault._revert_state = {
            "column": params["widened_column"],
            "separator": params["separator"],
        }
        return fault

    def _apply(self, spark: Any) -> None:
        from pyspark.sql import functions as F

        table = spark.table(self.target_item)

        # If the separator already occurs in the data, the transform cannot be
        # reversed — revert would truncate real values. Refuse rather than
        # corrupt the estate.
        contaminated = (
            table.filter(F.col(self.column).contains(self.separator)).limit(1).count()
        )

        if contaminated:
            raise ValueError(
                f"{self.column} already contains '{self.separator}'. "
                "Choose a separator absent from the data, or the fault "
                "cannot be reverted cleanly."
            )

        self._revert_state = {"column": self.column, "separator": self.separator}

        widened = table.withColumn(
            self.column,
            F.expr(widen_expression(self.column, self.uniquifier, self.separator)),
        )
        _overwrite(widened, self.target_item, schema_change=False)

    def _undo(self, spark: Any) -> None:
        from pyspark.sql import functions as F

        column = self._revert_state["column"]
        separator = self._revert_state["separator"]

        narrowed = spark.table(self.target_item).withColumn(
            column, F.expr(narrow_expression(column, separator))
        )
        _overwrite(narrowed, self.target_item, schema_change=False)


class SilentZeroRow(Fault):
    """An upstream export writes a header and no data.

    The most dangerous fault in the set, because nothing is broken. A filter
    matched nothing, an export failed after writing its header, a source system
    had an empty day — and the landing file arrives with its column names and no
    rows beneath them.

    The copy activity reads it, finds zero rows, writes zero rows, and reports
    success. The pipeline is green, the schedule is met, and every downstream
    report serves yesterday's figures until somebody notices a number looks
    stale.

    Verified: three scheduled runs after injection all completed in 19 to 23
    seconds, matching the baseline exactly, and no Delta version was written at
    all — Delta does not commit when there is nothing to write. Detection is
    therefore a join between pipeline run history and the transaction log, not a
    measurement of either. See D-025.
    """

    fault_class = "silent_zero_row"

    def __init__(
        self,
        target_workspace: str,
        target_item: str,
        landing_path: str = LANDING_PATH,
        backup_dir: str = BACKUP_DIR,
        seed: int = 4417,
    ):
        super().__init__(target_workspace, target_item, seed)
        self.landing_path = landing_path
        self.backup_dir = backup_dir

    def mechanism(self) -> str:
        return (
            f"The landing file feeding {self.target_item} was written with its "
            "header row and no data rows. The pipeline ingested it successfully "
            "and delivered nothing."
        )

    def expected_symptom(self) -> str:
        return (
            "Pipeline status Succeeded, duration normal, numOutputRows 0. "
            "Downstream data is unchanged and therefore stale. No error anywhere."
        )

    def parameters(self) -> dict[str, Any]:
        return {
            "landing_path": self.landing_path,
            "backup_path": self._backup_path,
            "pipeline": self.target_item,
        }

    @property
    def _backup_path(self) -> str:
        """Where the original file is kept. Outside the landing folder, so a
        pipeline reading the folder rather than a named file cannot pick it up."""
        return str(Path(self.backup_dir) / Path(self.landing_path).name)

    @classmethod
    def _rebuild(cls, truth: GroundTruth) -> "SilentZeroRow":
        params = truth.parameters
        backup = Path(params["backup_path"])

        fault = cls(
            truth.target_workspace,
            truth.target_item,
            landing_path=params["landing_path"],
            backup_dir=str(backup.parent),
            seed=truth.seed,
        )
        fault._revert_state = {
            "backup": params["backup_path"],
            "source": params["landing_path"],
        }
        return fault

    def _apply(self, spark: Any) -> None:
        """Spark is unused. This fault is filesystem-only.

        Must run inside a Fabric notebook with the lakehouse attached, since it
        writes through the /lakehouse/default mount.
        """
        source = Path(self.landing_path)

        if not source.exists():
            raise FileNotFoundError(
                f"No landing file at {source}. Generate one with "
                "estate.seed.landing.write_batch() before injecting."
            )

        header = source.read_text().splitlines()[0]

        backup = Path(self._backup_path)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)

        self._revert_state = {"backup": str(backup), "source": str(source)}

        # Header and a trailing newline. Nothing else.
        source.write_text(header + "\n")

    def _undo(self, spark: Any) -> None:
        backup = Path(self._revert_state["backup"])
        source = Path(self._revert_state["source"])

        if not backup.exists():
            raise FileNotFoundError(
                f"Backup missing at {backup}. The original landing file cannot "
                "be restored; regenerate it with landing.write_batch()."
            )

        shutil.copy2(backup, source)
        backup.unlink()


# --------------------------------------------------------------------------
# Not yet implementable. Each needs estate components that do not exist.
#
#   refresh_collision  needs at least two semantic models with schedules
#   contention         needs two concurrent workloads competing for capacity
#   gateway_timeout    needs an on-premises data gateway
# --------------------------------------------------------------------------

IMPLEMENTED = {
    SchemaDrift.fault_class: SchemaDrift,
    ColumnWidening.fault_class: ColumnWidening,
    SilentZeroRow.fault_class: SilentZeroRow,
}

PENDING = (
    "refresh_collision",
    "contention",
    "gateway_timeout",
)


def rebuild(truth: GroundTruth) -> Fault:
    """Reconstruct any fault from its sealed record, without knowing its class.

    The entry point for cleanup: given a sealed JSON file, this returns an object
    whose `revert(spark)` undoes the fault. Nothing else is needed — no live
    session, no memory of what was injected.

        truth = Seal().load(fault_id)
        rebuild(truth).revert(spark)
    """
    if truth.fault_class not in IMPLEMENTED:
        raise KeyError(
            f"No implementation for fault class '{truth.fault_class}'. "
            f"Known: {sorted(IMPLEMENTED)}"
        )

    return IMPLEMENTED[truth.fault_class].from_ground_truth(truth)