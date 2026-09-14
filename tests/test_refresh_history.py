"""Refresh history collector tests. No network, no credentials."""

from datetime import datetime, timezone

import pytest
import requests

from collectors import refresh_history


def test_dataset_parses_api_casing():
    dataset = refresh_history.Dataset.model_validate(
        {
            "id": "d1",
            "name": "MFI",
            "configuredBy": "contact@houndle.co",
            "isRefreshable": True,
            "targetStorageMode": "Abf",
            "unexpectedField": "ignored",
        }
    )
    assert dataset.name == "MFI"
    assert dataset.is_refreshable is True


def test_duration_is_computed():
    run = refresh_history.RefreshRun.model_validate(
        {
            "id": 1,
            "status": "Completed",
            "startTime": "2026-04-24T08:00:00Z",
            "endTime": "2026-04-24T08:02:30Z",
        }
    )
    assert run.duration_seconds == 150.0


def test_naive_timestamps_are_treated_as_utc():
    """Power BI omits the Z suffix on some responses. Same defect as D-007."""
    run = refresh_history.RefreshRun.model_validate(
        {"id": 1, "startTime": "2026-04-24T08:00:00", "endTime": "2026-04-24T08:02:30"}
    )
    assert run.start_time.tzinfo is not None
    assert run.duration_seconds == 150.0
    assert run.start_time < datetime.now(timezone.utc)


def test_in_progress_refresh_has_no_duration():
    run = refresh_history.RefreshRun.model_validate(
        {"id": 1, "status": "Unknown", "startTime": "2026-09-13T08:00:00Z"}
    )
    assert run.duration_seconds is None


def test_failed_refresh_keeps_its_error():
    run = refresh_history.RefreshRun.model_validate(
        {
            "id": 1,
            "status": "Failed",
            "startTime": "2026-04-24T08:00:00Z",
            "endTime": "2026-04-24T08:00:12Z",
            "serviceExceptionJson": '{"errorCode":"ModelRefreshFailed"}',
        }
    )
    assert "ModelRefreshFailed" in run.service_exception_json


@pytest.mark.parametrize("status_code", [400, 403, 404])
def test_expected_errors_yield_nothing(monkeypatch, status_code):
    """Non-refreshable or inaccessible models must not halt collection."""

    response = requests.Response()
    response.status_code = status_code

    def raise_http_error(path, **kwargs):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(refresh_history.powerbi_api, "get_list", raise_http_error)
    assert list(refresh_history.list_refreshes("g1", "d1")) == []


def test_server_errors_still_raise(monkeypatch):
    response = requests.Response()
    response.status_code = 500

    def raise_http_error(path, **kwargs):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(refresh_history.powerbi_api, "get_list", raise_http_error)

    with pytest.raises(requests.HTTPError):
        list(refresh_history.list_refreshes("g1", "d1"))


def test_non_refreshable_models_are_not_queried(monkeypatch):
    """DirectQuery models have no refresh history; don't waste a call."""
    monkeypatch.setattr(
        refresh_history, "list_groups", lambda: [{"id": "g1", "name": "ws"}]
    )
    monkeypatch.setattr(
        refresh_history,
        "list_datasets",
        lambda g: iter(
            [refresh_history.Dataset(id="d1", name="live", isRefreshable=False)]
        ),
    )

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("list_refreshes called on a non-refreshable model")

    monkeypatch.setattr(refresh_history, "list_refreshes", should_not_be_called)
    monkeypatch.setattr(refresh_history.storage, "write", lambda d, r: None)

    counts = refresh_history.collect()
    assert counts["datasets"] == 1
    assert counts["refresh_runs"] == 0


def test_collect_skips_refreshes_older_than_cutoff(monkeypatch):
    monkeypatch.setattr(
        refresh_history, "list_groups", lambda: [{"id": "g1", "name": "ws"}]
    )
    monkeypatch.setattr(
        refresh_history,
        "list_datasets",
        lambda g: iter(
            [refresh_history.Dataset(id="d1", name="MFI", isRefreshable=True)]
        ),
    )

    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc)

    monkeypatch.setattr(
        refresh_history,
        "list_refreshes",
        lambda g, d: iter(
            [
                refresh_history.RefreshRun(id=1, startTime=old),
                refresh_history.RefreshRun(id=2, startTime=recent),
            ]
        ),
    )

    written: dict[str, int] = {}
    monkeypatch.setattr(
        refresh_history.storage,
        "write",
        lambda dataset, rows: written.update({dataset: len(rows)}),
    )

    counts = refresh_history.collect(since_days=90)
    assert counts["refresh_runs"] == 1
    assert written["refresh_runs"] == 1

def test_unknown_refreshability_is_still_queried(monkeypatch):
    """The API returns None for a service principal. None must not mean skip."""
    monkeypatch.setattr(
        refresh_history, "list_groups", lambda: [{"id": "g1", "name": "ws"}]
    )
    monkeypatch.setattr(
        refresh_history,
        "list_datasets",
        lambda g: iter(
            [refresh_history.Dataset(id="d1", name="MFI", isRefreshable=None)]
        ),
    )
    monkeypatch.setattr(
        refresh_history,
        "list_refreshes",
        lambda g, d: iter([refresh_history.RefreshRun(id=1)]),
    )
    monkeypatch.setattr(refresh_history.storage, "write", lambda d, r: None)

    counts = refresh_history.collect(since_days=365)
    assert counts["refresh_runs"] == 1