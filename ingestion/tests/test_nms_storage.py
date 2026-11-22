"""Tests for batched NMS Databricks SQL persistence."""

from types import SimpleNamespace

import pytest
from databricks.sdk.service.sql import StatementState

from nfs_ingest.services.nms_parser import PreparedNmsRecord
from nfs_ingest.services.nms_storage import NmsDatabricksWriter


class FakeStatementExecution:
    def __init__(self, verification_counts: list[int]) -> None:
        self.verification_counts = verification_counts
        self.calls: list[dict] = []

    def execute_statement(self, **kwargs):
        self.calls.append(kwargs)
        statement = kwargs["statement"]
        rows = None
        if "SELECT count(*) AS matched" in statement:
            rows = [[str(self.verification_counts.pop(0))]]
        return SimpleNamespace(
            statement_id=f"statement-{len(self.calls)}",
            status=SimpleNamespace(
                state=StatementState.SUCCEEDED,
                error=None,
            ),
            result=SimpleNamespace(data_array=rows) if rows is not None else None,
        )


def client(counts: list[int]):
    return SimpleNamespace(statement_execution=FakeStatementExecution(counts))


def record(*, hazard: bool = True) -> PreparedNmsRecord:
    bronze = {
        "event_id": "event-1",
        "nms_id": "nms-1",
        "fetched_at": "2026-10-27T16:00:00+00:00",
        "last_updated_at": "2026-10-27T15:59:00+00:00",
        "request_mode": "incremental",
        "request_watermark": "2026-10-27T15:58:00+00:00",
        "payload_sha256": "hash",
        "payload_json": "{}",
        "source": "FAA_NMS",
    }
    version = {
        "notam_version_id": "version-1",
        "bronze_event_id": "event-1",
        "nms_id": "nms-1",
        "notam_id": "A1234",
        "notam_type": "N",
        "status": "ACTIVE",
        "location": "KJFK",
        "replaces_notam_version_id": None,
        "effective_start": "2026-10-27T15:00:00+00:00",
        "effective_end": "2026-10-27T18:00:00+00:00",
        "raw_text": "text",
        "category": None,
        "phase_of_flight": [],
        "valid_from": "2026-10-27T15:59:00+00:00",
        "valid_to": None,
        "is_current": True,
        "parsed_at": "2026-10-27T16:00:00+00:00",
        "parser_version": "test",
        "source": "FAA_NMS",
    }
    hazard_row = {
        "hazard_id": "hazard-1",
        "notam_version_id": "version-1",
        "nms_id": "nms-1",
        "notam_id": "A1234",
        "location": "KJFK",
        "effective_start": "2026-10-27T15:00:00+00:00",
        "effective_end": "2026-10-27T18:00:00+00:00",
        "geometry_geojson": '{"type":"Polygon","coordinates":[]}',
        "geometry_source": "NMS_GEOJSON_AREA",
        "lower_altitude_ft": 0.0,
        "upper_altitude_ft": 18000.0,
        "lower_altitude_reference": "SFC",
        "upper_altitude_reference": "STD",
        "horizontal_radius_nm": 0.0,
        "circle_center_longitude": None,
        "circle_center_latitude": None,
        "horizontal_buffer_nm": 0.0,
        "h3_resolution": 7,
        "parse_confidence": 1.0,
        "parse_warnings": [],
        "parser_version": "test",
        "source": "FAA_NMS",
        "created_at": "2026-10-27T16:00:00+00:00",
    }
    return PreparedNmsRecord(
        bronze=bronze,
        version=version,
        hazard=hazard_row if hazard else None,
    )


def test_writer_uses_ordered_idempotent_merges_then_verifies() -> None:
    fake_client = client([1, 1, 1])
    writer = NmsDatabricksWriter(
        client=fake_client,
        warehouse_id="warehouse",
    )

    result = writer.write([record()])

    statements = [call["statement"] for call in fake_client.statement_execution.calls]
    assert "nfs_nms_notam_events_raw" in statements[0]
    assert "target.is_current = false" in statements[1]
    assert "nfs_notam_versions" in statements[2]
    assert "h3_try_coverash3" in statements[3]
    assert all("MERGE INTO" in statement for statement in statements[:4])
    assert result == {
        "bronze_records": 1,
        "notam_versions": 1,
        "hazard_volumes": 1,
        "verified": True,
    }


def test_writer_skips_hazard_merge_when_geometry_is_unresolved() -> None:
    fake_client = client([1, 1])
    writer = NmsDatabricksWriter(
        client=fake_client,
        warehouse_id="warehouse",
    )

    result = writer.write([record(hazard=False)])

    statements = [call["statement"] for call in fake_client.statement_execution.calls]
    assert not any("h3_try_coverash3" in statement for statement in statements)
    assert result["hazard_volumes"] == 0


def test_writer_raises_when_verification_does_not_match() -> None:
    writer = NmsDatabricksWriter(
        client=client([0, 1, 1]),
        warehouse_id="warehouse",
    )

    with pytest.raises(RuntimeError, match="verification count"):
        writer.write([record()])
