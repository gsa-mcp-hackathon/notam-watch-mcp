"""Tests for transactional NMS ingestion orchestration."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from nfs_ingest.services.nms import (
    NmsClient,
    NmsInitialLoadRequired,
    _batches,
    _decode_feature_file,
    ingest_incremental,
    ingest_initial,
)
from nfs_ingest.services.nms_parser import prepare_nms_payload

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def feature(nms_id: str = "nms-1") -> dict:
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "coreNOTAMData": {
                "notam": {
                    "id": nms_id,
                    "type": "N",
                    "effectiveStart": "2026-10-27T15:00:00Z",
                    "lastUpdated": "2026-10-27T15:59:00Z",
                    "text": "RWY 04 CLOSED",
                }
            }
        },
    }


def payload() -> dict:
    return {"data": {"geojson": [feature()]}}


class FakeClient:
    def get_notams_since(self, since):
        self.since = since
        return payload(), "request-1"


class FakeBaselineClient:
    def __init__(self, count: int = 5) -> None:
        self.count = count
        self.requested = []

    def get_classification_baseline(self, classification):
        self.requested.append(classification)
        return [feature(f"nms-{i}") for i in range(self.count)], "request-il"


class FakeWriter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.records = []
        self.batches = []

    def write(self, records):
        if self.error:
            raise self.error
        self.records = records
        self.batches.append(len(records))
        return {
            "bronze_records": len(records),
            "notam_versions": len(records),
            "hazard_volumes": 0,
            "verified": True,
        }


class FakeWatermarks:
    def __init__(self, watermark):
        self.watermark = watermark
        self.events = []

    def get_last_successful_pull_at(self):
        return self.watermark

    def mark_started(self, plan):
        self.events.append(("started", plan))

    def mark_succeeded(self, plan, **kwargs):
        self.events.append(("succeeded", plan, kwargs))

    def mark_failed(self, plan, **kwargs):
        self.events.append(("failed", plan, kwargs))


def test_ingest_writes_before_advancing_watermark() -> None:
    state = FakeWatermarks(NOW - timedelta(minutes=10))
    writer = FakeWriter()

    result = ingest_incremental(
        writer=writer,
        watermark_store=state,
        client=FakeClient(),
        now=NOW,
    )

    assert [event[0] for event in state.events] == ["started", "succeeded"]
    assert state.events[1][2]["advance_watermark"] is True
    assert result["watermark_advanced"] is True
    assert len(writer.records) == 1


def test_shadow_ingest_persists_without_advancing_watermark() -> None:
    state = FakeWatermarks(None)

    result = ingest_incremental(
        writer=FakeWriter(),
        watermark_store=state,
        client=FakeClient(),
        since=NOW - timedelta(minutes=2),
        advance_watermark=False,
        now=NOW,
    )

    assert state.events[1][2]["advance_watermark"] is False
    assert result["state"] == "shadow-succeeded"


def test_failed_write_marks_failure_without_success() -> None:
    state = FakeWatermarks(NOW - timedelta(minutes=10))

    with pytest.raises(RuntimeError, match="write failed"):
        ingest_incremental(
            writer=FakeWriter(RuntimeError("write failed")),
            watermark_store=state,
            client=FakeClient(),
            now=NOW,
        )

    assert [event[0] for event in state.events] == ["started", "failed"]


def test_missing_baseline_refuses_to_advance_incrementally() -> None:
    state = FakeWatermarks(None)

    with pytest.raises(NmsInitialLoadRequired, match="initial load"):
        ingest_incremental(
            writer=FakeWriter(),
            watermark_store=state,
            client=FakeClient(),
            now=NOW,
        )

    assert state.events == []


def test_initial_load_writes_batches_then_advances_watermark() -> None:
    state = FakeWatermarks(None)
    writer = FakeWriter()
    client = FakeBaselineClient(count=5)

    result = ingest_initial(
        writer=writer,
        watermark_store=state,
        client=client,
        classifications=["domestic"],
        now=NOW,
        batch_size=2,
    )

    assert client.requested == ["DOMESTIC"]
    assert writer.batches == [2, 2, 1]
    assert [event[0] for event in state.events] == ["started", "succeeded"]
    assert state.events[0][1].mode == "initial"
    assert state.events[1][2]["advance_watermark"] is True
    assert state.events[1][2]["records_written"] == 5
    assert result["bronze_records"] == 5
    assert result["classifications"]["DOMESTIC"]["features_downloaded"] == 5
    assert result["commit_watermark_at"] == NOW.isoformat()


def test_default_baseline_includes_domestic_and_fdc() -> None:
    state = FakeWatermarks(None)
    client = FakeBaselineClient(count=2)

    result = ingest_initial(
        writer=FakeWriter(),
        watermark_store=state,
        client=client,
        now=NOW,
    )

    assert client.requested == ["DOMESTIC", "FDC"]
    assert set(result["classifications"]) == {"DOMESTIC", "FDC"}
    assert [event[0] for event in state.events] == ["started", "succeeded"]


def test_initial_load_failure_does_not_advance_watermark() -> None:
    state = FakeWatermarks(None)

    with pytest.raises(RuntimeError, match="write failed"):
        ingest_initial(
            writer=FakeWriter(RuntimeError("write failed")),
            watermark_store=state,
            client=FakeBaselineClient(),
            classifications=["DOMESTIC"],
            now=NOW,
        )

    assert [event[0] for event in state.events] == ["started", "failed"]


def test_initial_load_rejects_unknown_classification() -> None:
    state = FakeWatermarks(None)

    with pytest.raises(ValueError, match="Unsupported"):
        ingest_initial(
            writer=FakeWriter(),
            watermark_store=state,
            client=FakeBaselineClient(),
            classifications=["MARS"],
            now=NOW,
        )

    assert state.events == []


def test_batches_respect_parameter_size_limit() -> None:
    records = prepare_nms_payload(
        {"data": {"geojson": [feature(f"nms-{i}") for i in range(10)]}},
        fetched_at=NOW,
        request_mode="initial",
        request_watermark=None,
    )
    one = len(json.dumps(records[0].bronze, separators=(",", ":")))

    batches = list(_batches(records, size=1000, max_chars=one * 3 + 10))

    assert sum(len(batch) for batch in batches) == 10
    assert all(len(batch) <= 3 for batch in batches)
    assert len(batches) >= 4


def test_batches_reject_single_oversized_record() -> None:
    records = prepare_nms_payload(
        payload(),
        fetched_at=NOW,
        request_mode="initial",
        request_watermark=None,
    )

    with pytest.raises(ValueError, match="parameter limit"):
        list(_batches(records, size=10, max_chars=50))


def test_decode_feature_file_accepts_gzip_list_and_feature_collection() -> None:
    features = [feature("a"), feature("b")]
    as_list = gzip.compress(json.dumps(features).encode("utf-8"))
    as_collection = json.dumps(
        {"type": "FeatureCollection", "features": features}
    ).encode("utf-8")

    assert len(_decode_feature_file(as_list)) == 2
    assert len(_decode_feature_file(as_collection)) == 2


class FakeResponse:
    def __init__(self, *, json_body=None, content=b"", headers=None) -> None:
        self._json = json_body
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._json


class FakeSession:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, **kwargs):
        return FakeResponse(json_body={"access_token": "token-1"})

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/notams"):
            return FakeResponse(
                json_body={
                    "status": "Success",
                    "data": {"url": "/nmsapi/v1/content/abc"},
                },
                headers={"x-request-id": "rid-1"},
            )
        return FakeResponse(
            content=gzip.compress(json.dumps([feature()]).encode("utf-8"))
        )


def test_classification_baseline_downloads_content_path(monkeypatch) -> None:
    from nfs_ingest.config import settings

    monkeypatch.setattr(settings, "nms_client_id", "id")
    monkeypatch.setattr(settings, "nms_client_secret", "secret")
    monkeypatch.setattr(
        settings, "nms_api_base_url", "https://nms.example/nmsapi/v1"
    )
    session = FakeSession()

    features, request_id = NmsClient(session).get_classification_baseline("domestic")

    assert request_id == "rid-1"
    assert len(features) == 1
    first_url, first_kwargs = session.calls[0]
    assert first_url == "https://nms.example/nmsapi/v1/notams"
    assert first_kwargs["params"] == {
        "classification": "DOMESTIC",
        "allowRedirect": "false",
    }
    assert first_kwargs["headers"]["nmsResponseFormat"] == "GEOJSON"
    assert session.calls[1][0] == "https://nms.example/nmsapi/v1/content/abc"
