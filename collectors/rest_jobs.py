"""Collect item inventory and job run history from the Fabric REST API.

Run:  python -m collectors.rest_jobs

Two datasets come out of this:
  items      — what exists in each workspace, and when it last changed
  job_runs   — every scheduled or manual run, with duration and failure reason

`job_runs` is the backbone of reliability detection. Duration anomalies, silent
successes and refresh collisions are all derived from it.

Note: semantic model refreshes are NOT in the Fabric jobs API. They live in the
Power BI refresh history endpoint and are collected separately by
`collectors.model_stats`. This collector covers the schedulable Fabric item types.
"""

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import requests
from pydantic import BaseModel, Field, field_validator

from common import storage
from common.fabric_api import get_paged

# Item types the /jobs/instances endpoint supports. Others 400 or return nothing.
JOB_CAPABLE_TYPES = {
    "DataPipeline",
    "Notebook",
    "SparkJobDefinition",
    "Dataflow",
    "Lakehouse",
}


class Item(BaseModel):
    """A Fabric item as returned by /workspaces/{id}/items."""

    id: str
    display_name: str = Field(alias="displayName")
    type: str
    description: str | None = None
    workspace_id: str = Field(alias="workspaceId")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class JobRun(BaseModel):
    """One execution of an item. Field names follow the API, snake_cased."""

    id: str
    item_id: str = Field(alias="itemId")
    job_type: str | None = Field(default=None, alias="jobType")
    invoke_type: str | None = Field(default=None, alias="invokeType")
    status: str | None = None
    start_time_utc: datetime | None = Field(default=None, alias="startTimeUtc")
    end_time_utc: datetime | None = Field(default=None, alias="endTimeUtc")
    failure_reason: dict[str, Any] | None = Field(
        default=None, alias="failureReason"
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @field_validator("start_time_utc", "end_time_utc", mode="after")
    @classmethod
    def assume_utc(cls, value: datetime | None) -> datetime | None:
        """The API omits the timezone marker on some responses.

        Without this, naive and aware datetimes end up mixed in the same field
        and any comparison between them raises TypeError. These timestamps are
        documented as UTC, so attaching the timezone is safe.
        """
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def duration_seconds(self) -> float | None:
        if self.start_time_utc and self.end_time_utc:
            return (self.end_time_utc - self.start_time_utc).total_seconds()
        return None


def list_workspaces() -> list[dict[str, Any]]:
    return list(get_paged("/workspaces"))


def list_items(workspace_id: str) -> Iterator[Item]:
    for raw in get_paged(f"/workspaces/{workspace_id}/items"):
        yield Item.model_validate(raw)


def list_job_runs(workspace_id: str, item_id: str) -> Iterator[JobRun]:
    """Job history for one item.

    A 404 or 400 here means the item type has no job concept, which is normal
    and not an error worth surfacing.
    """
    path = f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances"

    try:
        for raw in get_paged(path):
            yield JobRun.model_validate(raw)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400, 404):
            return
        raise


def collect(since_days: int = 30) -> dict[str, int]:
    """Walk every visible workspace and write both datasets. Returns row counts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    item_rows: list[dict[str, Any]] = []
    run_rows: list[dict[str, Any]] = []

    for workspace in list_workspaces():
        ws_id = workspace["id"]
        ws_name = workspace.get("displayName", "")

        for item in list_items(ws_id):
            item_rows.append(
                {
                    "workspace_id": ws_id,
                    "workspace_name": ws_name,
                    "item_id": item.id,
                    "item_name": item.display_name,
                    "item_type": item.type,
                }
            )

            if item.type not in JOB_CAPABLE_TYPES:
                continue

            for run in list_job_runs(ws_id, item.id):
                if run.start_time_utc and run.start_time_utc < cutoff:
                    continue

                run_rows.append(
                    {
                        "workspace_id": ws_id,
                        "workspace_name": ws_name,
                        "item_id": item.id,
                        "item_name": item.display_name,
                        "item_type": item.type,
                        "run_id": run.id,
                        "job_type": run.job_type,
                        "invoke_type": run.invoke_type,
                        "status": run.status,
                        "start_time_utc": run.start_time_utc,
                        "end_time_utc": run.end_time_utc,
                        "duration_seconds": run.duration_seconds,
                        "failure_reason": (
                            str(run.failure_reason) if run.failure_reason else None
                        ),
                    }
                )

    storage.write("items", item_rows)
    storage.write("job_runs", run_rows)

    return {"items": len(item_rows), "job_runs": len(run_rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Ignore runs older than this. Default 30.",
    )
    args = parser.parse_args()

    counts = collect(since_days=args.since_days)

    print(f"items:    {counts['items']}")
    print(f"job_runs: {counts['job_runs']}")

    if counts["job_runs"] == 0:
        print("\nNo job runs found. Expected on an estate with no pipelines yet.")


if __name__ == "__main__":
    main()