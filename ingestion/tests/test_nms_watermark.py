"""Tests for NMS restart and catch-up planning."""

from datetime import UTC, datetime, timedelta

import pytest

from nfs_ingest.config import settings
from nfs_ingest.services.nms_watermark import plan_sync

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def test_missing_watermark_requires_initial_load() -> None:
    plan = plan_sync(None, now=NOW)

    assert plan.mode == "initial"
    assert plan.query_since is None
    assert plan.commit_watermark_at == NOW


def test_recent_watermark_catches_up_with_overlap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nms_incremental_overlap_minutes", 2)
    watermark = NOW - timedelta(hours=3)

    plan = plan_sync(watermark, now=NOW)

    assert plan.mode == "incremental"
    assert plan.query_since == watermark - timedelta(minutes=2)
    assert plan.commit_watermark_at == NOW


def test_old_watermark_rebaselines_after_incremental_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nms_incremental_max_lookback_hours", 24)

    plan = plan_sync(NOW - timedelta(hours=25), now=NOW)

    assert plan.mode == "initial"
    assert plan.query_since is None


def test_overlap_is_clamped_to_incremental_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nms_incremental_max_lookback_hours", 24)
    monkeypatch.setattr(settings, "nms_incremental_overlap_minutes", 5)
    watermark = NOW - timedelta(hours=23, minutes=59)

    plan = plan_sync(watermark, now=NOW)

    assert plan.mode == "incremental"
    assert plan.query_since == NOW - timedelta(hours=24)


def test_future_watermark_is_rejected() -> None:
    with pytest.raises(ValueError, match="later than"):
        plan_sync(NOW + timedelta(seconds=1), now=NOW)


def test_naive_watermark_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        plan_sync(datetime(2026, 10, 27, 12, 0), now=NOW)
