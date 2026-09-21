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
"""

from __future__ import annotations

from typing import Any

from estate.faults.base import Fault

# A free-text narrative field. High cardinality by construction: the whole point
# is that it cannot compress. Assembled from fragments so the generated values
# look like real operator notes rather than random characters.
NARRATIVE_FRAGMENTS = (
    "Client attended branch",
    "Disbursement confirmed by",
    "Repayment schedule adjusted following",
    "Late payment flagged, contacted via",
    "Documentation incomplete at time of",
    "Group guarantor signature obtained for",
    "Amount revised after review by",
    "Rescheduled at client request due to",
)


def _overwrite(frame: Any, table: str) -> None:
    """Replace a Delta table's contents and schema in one commit.

    `overwriteSchema` is what allows a column to be renamed, added or dropped.
    Delta's snapshot isolation is what makes it safe to read and overwrite the
    same table in one job.
    """
    (
        frame.write.mode("overwrite")
        .option("overwriteSchema", "true")
        .format("delta")
        .saveAsTable(table)
    )


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
        _overwrite(renamed, self.target_item)

    def _undo(self, spark: Any) -> None:
        restored = spark.table(self.target_item).withColumnRenamed(
            self._revert_state["from"], self._revert_state["to"]
        )
        _overwrite(restored, self.target_item)


class ModelBloat(Fault):
    """An upstream change adds a high-cardinality column nobody asked for.

    Reproduces INC-0412 from the design mockup. The column is added at the source;
    a semantic model importing columns by wildcard picks it up without any human
    deciding to include it, and the model's size and refresh time multiply.

    Nothing reads it. That is what makes the diagnosis non-obvious: the column is
    expensive precisely because it is useless, and useless columns attract no
    attention.
    """

    fault_class = "model_bloat"

    def __init__(
        self,
        target_workspace: str,
        target_item: str,
        column: str = "TransactionNarrative",
        seed: int = 4417,
    ):
        super().__init__(target_workspace, target_item, seed)
        self.column = column

    def mechanism(self) -> str:
        return (
            f"A high-cardinality free-text column {self.column} was added to "
            f"{self.target_item} upstream. It has no downstream consumer and "
            f"cannot compress."
        )

    def expected_symptom(self) -> str:
        return (
            "Table and dependent model size increase sharply; refresh duration "
            "rises in a single step rather than drifting."
        )

    def parameters(self) -> dict[str, Any]:
        return {"added_column": self.column, "cardinality": "near-unique"}

    def _apply(self, spark: Any) -> None:
        from pyspark.sql import functions as F

        self._revert_state = {"column": self.column}

        fragments = F.array(*[F.lit(f) for f in NARRATIVE_FRAGMENTS])

        # Narrative = a fragment plus the transaction's own ID. The ID makes every
        # value distinct, which is what defeats dictionary compression.
        narrative = F.concat(
            F.element_at(
                fragments,
                (F.abs(F.hash(F.col("TransactionID"))) % len(NARRATIVE_FRAGMENTS)) + 1,
            ),
            F.lit(" ref "),
            F.col("TransactionID"),
            F.lit(" on "),
            F.date_format(F.col("TransactionDate"), "yyyy-MM-dd"),
        )

        bloated = spark.table(self.target_item).withColumn(self.column, narrative)
        _overwrite(bloated, self.target_item)

    def _undo(self, spark: Any) -> None:
        trimmed = spark.table(self.target_item).drop(self._revert_state["column"])
        _overwrite(trimmed, self.target_item)


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
    ModelBloat.fault_class: ModelBloat,
}

PENDING = (
    "silent_zero_row",
    "refresh_collision",
    "contention",
    "gateway_timeout",
)