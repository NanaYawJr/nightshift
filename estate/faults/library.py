"""Fault implementations that operate on Delta tables.

Only two of the six classes can act on the estate as it currently stands, because
the other four need pipelines, semantic models or a gateway — none of which exist
yet. They are listed at the bottom as stubs so the gap is visible in code rather
than only in a planning document.
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


class SchemaDrift(Fault):
    """An upstream source renames a column without telling anyone.

    The canonical silent failure: everything downstream that expects the old name
    either fails loudly, or — far worse — is configured to skip what it cannot
    find and quietly delivers nothing.
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

        spark.sql(
            f"ALTER TABLE {self.target_item} "
            f"RENAME COLUMN {self.column} TO {self.new_name}"
        )

    def _undo(self, spark: Any) -> None:
        spark.sql(
            f"ALTER TABLE {self.target_item} "
            f"RENAME COLUMN {self._revert_state['from']} "
            f"TO {self._revert_state['to']}"
        )


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

        (
            spark.table(self.target_item)
            .withColumn(self.column, narrative)
            .write.mode("overwrite")
            .option("overwriteSchema", "true")
            .format("delta")
            .saveAsTable(self.target_item)
        )

    def _undo(self, spark: Any) -> None:
        (
            spark.table(self.target_item)
            .drop(self._revert_state["column"])
            .write.mode("overwrite")
            .option("overwriteSchema", "true")
            .format("delta")
            .saveAsTable(self.target_item)
        )


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