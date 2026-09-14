"""Collect semantic model inventory and refresh history from the Power BI API.

Run:  python -m collectors.refresh_history

Two datasets come out of this:
  datasets          — every semantic model, its mode, and its refresh schedule
  refresh_runs      — every refresh attempt, with duration and failure detail

This is the reliability backbone for semantic models, the way `job_runs` is for
pipelines and notebooks. Refresh duration anomalies, refresh window collisions
and stale models are all derived from `refresh_runs`.

Refresh history is NOT available through the Fabric API — it lives only here.
VertiPaq and Best Practice Analyzer statistics are a third source again, and
require the Fabric notebook runtime (see `notebooks/`), so they are not
collected by this module.
"""

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests
from pydantic import BaseModel, Field, field_validator

from common import powerbi_api, storage

# Refresh history is capped server-side. 60 covers a month of daily refreshes
# with headroom for models that refresh more than once a day.
HISTORY_LIMIT = 60


class Dataset(BaseModel):
    """A semantic model as returned by /groups/{id}/datasets."""

    id: str
    name: str
    configured_by: str | None = Field(default=None, alias="configuredBy")
    is_refreshable: bool | None = Field(default=None, alias="isRefreshable")
    target_storage_mode: str | None = Field(
        default=None, alias="targetStorageMode"
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RefreshRun(BaseModel):
    """One refresh attempt against a semantic model."""

    id: int | str
    request_id: str | None = Field(default=None, alias="requestId")
    refresh_type: str | None = Field(default=None, alias="refreshType")
    status: str | None = None
    start_time: datetime | None = Field(default=None, alias="startTime")
    end_time: datetime | None = Field(default=None, alias="endTime")
    service_exception_json: str | None = Field(
        default=None, alias="serviceExceptionJson"
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def assume_utc(cls, value: datetime | None) -> datetime | None:
        """Power BI returns UTC but does not always mark it.

        Same defect as the Fabric jobs API (see D-007). Without this, naive and
        aware datetimes mix in one field and comparisons raise TypeError.
        """
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def duration_seconds(self) -> float | None:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None


def list_groups() -> list[dict[str, Any]]:
    """Workspaces, in Power BI's vocabulary."""
    return powerbi_api.get_list("/groups")


def list_datasets(group_id: str) -> Iterator[Dataset]:
    for raw in powerbi_api.get_list(f"/groups/{group_id}/datasets"):
        yield Dataset.model_validate(raw)


def list_refreshes(group_id: str, dataset_id: str) -> Iterator[RefreshRun]:
    """Refresh history for one model.

    A 403 means the principal lacks permission on this model; a 400 usually
    means the model is not refreshable (DirectQuery, or a push dataset).
    Neither is an error worth halting the whole collection for.
    """
    path = f"/groups/{group_id}/datasets/{dataset_id}/refreshes"

    try:
        for raw in powerbi_api.get_list(path, params={"$top": HISTORY_LIMIT}):
            yield RefreshRun.model_validate(raw)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400, 403, 404):
            return
        raise


def collect(since_days: int = 90) -> dict[str, int]:
    """Walk every visible workspace and write both datasets. Returns row counts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    dataset_rows: list[dict[str, Any]] = []
    refresh_rows: list[dict[str, Any]] = []

    for group in list_groups():
        group_id = group["id"]
        group_name = group.get("name", "")

        for dataset in list_datasets(group_id):
            dataset_rows.append(
                {
                    "workspace_id": group_id,
                    "workspace_name": group_name,
                    "dataset_id": dataset.id,
                    "dataset_name": dataset.name,
                    "configured_by": dataset.configured_by,
                    "is_refreshable": dataset.is_refreshable,
                    "storage_mode": dataset.target_storage_mode,
                }
            )
# The API returns None for a service principal rather than True.
            # Only skip when explicitly told the model cannot refresh.
            if dataset.is_refreshable is False:
                continue

            for run in list_refreshes(group_id, dataset.id):
                if run.start_time and run.start_time < cutoff:
                    continue

                refresh_rows.append(
                    {
                        "workspace_id": group_id,
                        "workspace_name": group_name,
                        "dataset_id": dataset.id,
                        "dataset_name": dataset.name,
                        "refresh_id": str(run.id),
                        "request_id": run.request_id,
                        "refresh_type": run.refresh_type,
                        "status": run.status,
                        "start_time": run.start_time,
                        "end_time": run.end_time,
                        "duration_seconds": run.duration_seconds,
                        "error": run.service_exception_json,
                    }
                )

    storage.write("datasets", dataset_rows)
    storage.write("refresh_runs", refresh_rows)

    return {"datasets": len(dataset_rows), "refresh_runs": len(refresh_rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=90,
        help="Ignore refreshes older than this. Default 90.",
    )
    args = parser.parse_args()

    counts = collect(since_days=args.since_days)

    print(f"datasets:     {counts['datasets']}")
    print(f"refresh_runs: {counts['refresh_runs']}")

    if counts["refresh_runs"] == 0:
        print(
            "\nNo refreshes in the window. Either nothing is scheduled, or the "
            "principal needs a higher workspace role than Viewer."
        )


if __name__ == "__main__":
    main()