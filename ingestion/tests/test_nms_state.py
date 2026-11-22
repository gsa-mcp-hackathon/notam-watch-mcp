"""Tests for the SQL helpers and the NMS watermark store used by the job."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nfs_ingest.services.nms_state import (
    _qualified_identifier,
    _string_literal,
    _timestamp_literal,
)
from nfs_ingest.services.nms_storage import NmsSqlWatermarkStore
from nfs_ingest.services.nms_watermark import plan_sync

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def test_rejects_unqualified_table_name() -> None:
    with pytest.raises(ValueError, match="three-part"):
        _qualified_identifier("watermarks")
    with pytest.raises(ValueError, match="three-part"):
        _qualified_identifier("users.schema.table; DROP TABLE x")


def test_quotes_personal_schema_with_hyphen() -> None:
    assert _qualified_identifier(
        "users.demo-schema.nfs_ingestion_watermarks"
    ) == "`users`.`demo-schema`.`nfs_ingestion_watermarks`"


def test_string_literal_escapes_quotes() -> None:
    assert _string_literal(None) == "NULL"
    assert _string_literal("O'Hare") == "'O''Hare'"


def test_timestamp_literal_is_utc_and_requires_timezone() -> None:
    assert _timestamp_literal(None) == "NULL"
    assert _timestamp_literal(NOW) == "TIMESTAMP '2026-10-27 16:00:00.000000'"
    with pytest.raises(ValueError, match="timezone"):
        _timestamp_literal(NOW.replace(tzinfo=None))


class RecordingStore(NmsSqlWatermarkStore):
    """Watermark store that records SQL instead of calling a warehouse."""

    def __init__(self, rows: list[list] | None = None) -> None:
        super().__init__(warehouse_id="test-warehouse", client=object())
        self.rows = rows or []
        self.statements: list[str] = []

    def _execute(self, statement: str):
        self.statements.append(statement)
        return SimpleNamespace(result=SimpleNamespace(data_array=self.rows))


def test_store_reads_existing_watermark_as_utc() -> None:
    store = RecordingStore([["2026-10-27T16:00:00.000Z"]])
    assert store.get_last_successful_pull_at() == NOW
    assert RecordingStore([]).get_last_successful_pull_at() is None


def test_failed_run_does_not_advance_successful_watermark() -> None:
    store = RecordingStore()
    store.mark_failed(
        plan_sync(NOW, now=NOW), error_detail="provider unavailable", completed_at=NOW
    )
    merge = store.statements[-1]
    assert "target.last_successful_pull_at = target.last_successful_pull_at" in merge
    assert "'failed'" in merge


def test_successful_run_advances_to_run_start() -> None:
    store = RecordingStore()
    store.mark_succeeded(
        plan_sync(None, now=NOW),
        request_id="request-1",
        records_written=42,
        completed_at=NOW,
    )
    merge = store.statements[-1]
    assert (
        "target.last_successful_pull_at = TIMESTAMP '2026-10-27 16:00:00.000000'"
        in merge
    )
    assert "'succeeded'" in merge
    assert "42" in merge


def test_shadow_run_records_success_without_advancing() -> None:
    store = RecordingStore()
    store.mark_succeeded(
        plan_sync(NOW, now=NOW),
        request_id=None,
        records_written=1,
        completed_at=NOW,
        advance_watermark=False,
    )
    merge = store.statements[-1]
    assert "target.last_successful_pull_at = target.last_successful_pull_at" in merge
    assert "'shadow-succeeded'" in merge
