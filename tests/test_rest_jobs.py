"""Collector tests. No network, no credentials — recorded responses only."""

from datetime import datetime, timezone

import pytest
import requests

from collectors import rest_jobs


def test_item_parses_api_casing():
    item = rest_jobs.Item.model_validate(
        {
            "id": "abc",
            "displayName": "pl_ingest_ledger_daily",
            "type": "DataPipeline",
            "workspaceId": "ws1",
            "unexpectedField": "ignored",
        }
    )
    assert item.display_name == "pl_ingest_ledger_daily"
    assert item.type == "DataPipeline"


def test_duration_is_computed_from_timestamps():
    run = rest_jobs.JobRun.model_validate(
        {
            "id": "run1",
            "itemId": "abc",
            "status": "Completed",
            "startTimeUtc": "2026-09-11T02:00:00Z",
            "endTimeUtc": "2026-09-11T02:10:30Z",
        }
    )
    assert run.duration_seconds == 630.0


def test_duration_is_none_while_running():
    run = rest_jobs.JobRun.model_validate(
        {"id": "run1", "itemId": "abc", "status": "InProgress"}
    )
    assert run.duration_seconds is None


def test_naive_timestamps_are_treated_as_utc():
    """The live API omits the Z suffix on some responses.

    Regression: mixing naive and aware datetimes raised TypeError on the
    cutoff comparison in collect().
    """
    run = rest_jobs.JobRun.model_validate(
        {
            "id": "run1",
            "itemId": "abc",
            "startTimeUtc": "2026-09-11T02:00:00",
            "endTimeUtc": "2026-09-11T02:10:30",
        }
    )
    assert run.start_time_utc.tzinfo is not None
    assert run.end_time_utc.tzinfo is not None
    assert run.duration_seconds == 630.0


def test_naive_timestamp_is_comparable_to_aware_cutoff():
    """The exact comparison that failed in production."""
    run = rest_jobs.JobRun.model_validate(
        {"id": "run1", "itemId": "abc", "startTimeUtc": "2020-01-01T00:00:00"}
    )
    cutoff = datetime.now(timezone.utc)
    assert run.start_time_utc < cutoff


def test_unsupported_item_type_yields_nothing(monkeypatch):
    """A 400 or 404 from /jobs/instances is normal, not a failure."""

    response = requests.Response()
    response.status_code = 400

    def raise_http_error(path, key="value"):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(rest_jobs, "get_paged", raise_http_error)
    assert list(rest_jobs.list_job_runs("ws1", "item1")) == []


def test_real_errors_still_raise(monkeypatch):
    """A 500 must not be swallowed by the same handler."""

    response = requests.Response()
    response.status_code = 500

    def raise_http_error(path, key="value"):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(rest_jobs, "get_paged", raise_http_error)

    with pytest.raises(requests.HTTPError):
        list(rest_jobs.list_job_runs("ws1", "item1"))


def test_collect_skips_runs_older_than_cutoff(monkeypatch):
    monkeypatch.setattr(
        rest_jobs,
        "list_workspaces",
        lambda: [{"id": "ws1", "displayName": "hl-finance-prod"}],
    )
    monkeypatch.setattr(
        rest_jobs,
        "list_items",
        lambda ws: iter(
            [
                rest_jobs.Item(
                    id="i1",
                    displayName="pl_daily",
                    type="DataPipeline",
                    workspaceId="ws1",
                )
            ]
        ),
    )

    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc)

    monkeypatch.setattr(
        rest_jobs,
        "list_job_runs",
        lambda ws, item: iter(
            [
                rest_jobs.JobRun(id="old", itemId="i1", startTimeUtc=old),
                rest_jobs.JobRun(id="new", itemId="i1", startTimeUtc=recent),
            ]
        ),
    )

    written: dict[str, int] = {}
    monkeypatch.setattr(
        rest_jobs.storage,
        "write",
        lambda dataset, rows: written.update({dataset: len(rows)}),
    )

    counts = rest_jobs.collect(since_days=30)

    assert counts["items"] == 1
    assert counts["job_runs"] == 1
    assert written["job_runs"] == 1


def test_collect_handles_runs_with_no_start_time(monkeypatch):
    """A queued run has no start time and must not be dropped or crash."""
    monkeypatch.setattr(
        rest_jobs, "list_workspaces", lambda: [{"id": "ws1", "displayName": "ws"}]
    )
    monkeypatch.setattr(
        rest_jobs,
        "list_items",
        lambda ws: iter(
            [
                rest_jobs.Item(
                    id="i1", displayName="nb", type="Notebook", workspaceId="ws1"
                )
            ]
        ),
    )
    monkeypatch.setattr(
        rest_jobs,
        "list_job_runs",
        lambda ws, item: iter(
            [rest_jobs.JobRun(id="queued", itemId="i1", status="NotStarted")]
        ),
    )
    monkeypatch.setattr(rest_jobs.storage, "write", lambda dataset, rows: None)

    counts = rest_jobs.collect(since_days=30)
    assert counts["job_runs"] == 1