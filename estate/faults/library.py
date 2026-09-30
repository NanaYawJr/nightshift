"""Fault implementations that operate on Delta tables.

Only two of the six classes can act on the estate as it currently stands, because
the other four need pipelines, semantic models or a gateway — none of which exist
yet. They are listed at the bottom as stubs so the gap is visible in code rather
than only in a planning document.

Both faults work by rewriting the table rather than altering it in place. In-place
column renames require Delta column mapping, which is a one-way protocol upgrade
that some readers of the table may not support. A rewrite needs no upgrade and is
closer to how these faults arrive in real estates anyway: an upstream source sends
a new full load in a new shape.

`model_bloat` is implemented by widening an existing column, not by adding one.
An earlier implementation added a high-cardinality column upstream and expected a
semantic model importing the whole table to pick it up. It does not: a Power BI
Import model pins its column list at publish time, storing each column with an
explicit `sourceColumn`, and refresh re-reads only those. Verified against a real
Import model — the added column never appeared. Widening an existing column
changes no schema at all, so the model imports exactly the columns it always did
and one of them silently stops compressing.
"""

from __future__ import annotations

from typing import Any

from estate.faults.base import Fault


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

    The canonical silent failure: everything downstream that expects the old name
    either fails loudly, or — far worse — is configured to skip what it cannot
    find and quietly delivers nothing.

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

    The mechanism behind INC-0412, corrected. A column that repeats compresses
    well: the engine stores each distinct value once and points at it. When the
    source begins writing a unique value per row — appending a reference, a
    timestamp, a correlation id — the dictionary stops helping and the column's
    cost rises sharply.

    Nothing about the schema changes. The column count is identical, the types are
    identical, the name is identical. A schema diff sees nothing. Only cardinality
    moves, which is why diagnosing this requires comparing against history rather
    than inspecting the current state.

    More realistic than adding a column, too. Source systems change what they put
    in a field far more often than they change the fields themselves.
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

    def _apply(self, spark: Any) -> None:
        from pyspark.sql import functions as F

        table = spark.table(self.target_item)

        # If the separator already occurs in the data, the transform cannot be
        # reversed — revert would truncate real values. Refuse rather than
        # corrupt the estate.
        contaminated = table.filter(
            F.col(self.column).contains(self.separator)
        ).limit(1).count()

        if contaminated:
            raise ValueError(
                f"{self.column} already contains '{self.separator}'. "
                "Choose a separator absent from the data, or the fault "
                "cannot be reverted cleanly."
            )

        self._revert_state = {
            "column": self.column,
            "separator": self.separator,
        }

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


# --------------------------------------------------------------------------
# Not yet implementable. Each needs estate components that do not exist.
#
#   silent_zero_row    needs a Data Factory pipeline with a copy activity
#                      configured to skip incompatible rows
#   refresh_collision  needs at least two semantic models with schedules
#   contention         needs two concurrent workloads competing for capacity
#   gateway_timeout    needs an on-premises data gateway
#
# Listed here rather than only in the plan so the gap is visible in the code.
# --------------------------------------------------------------------------

IMPLEMENTED = {
    SchemaDrift.fault_class: SchemaDrift,
    ColumnWidening.fault_class: ColumnWidening,
}

PENDING = (
    "silent_zero_row",
    "refresh_collision",
    "contention",
    "gateway_timeout",
)