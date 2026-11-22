"""Tests for SWIM retention cutoff logic (pure, no Spark)."""

from datetime import UTC, datetime, timedelta

from nfs_ingest.services.retention import (
    RetentionPolicy,
    compute_cutoff,
    default_policies,
)

NEWEST = datetime(2026, 10, 29, 2, 29, tzinfo=UTC)
BRONZE = RetentionPolicy("c.s.bronze", "received_at", 12, gold_gated=True)
CURRENT = RetentionPolicy("c.s.current", "observed_at", 2, gold_gated=False)


def test_cutoff_is_relative_to_table_max_not_wall_clock() -> None:
    paused_feed_newest = NEWEST - timedelta(days=3)

    decision = compute_cutoff(
        BRONZE, paused_feed_newest, require_gold=False, gold_watermark=None
    )

    assert decision.cutoff == paused_feed_newest - timedelta(hours=12)
    assert decision.gated_by_gold is False
    assert decision.skipped_reason is None


def test_naive_max_timestamp_is_treated_as_utc() -> None:
    decision = compute_cutoff(
        CURRENT,
        NEWEST.replace(tzinfo=None),
        require_gold=False,
        gold_watermark=None,
    )

    assert decision.cutoff == NEWEST - timedelta(hours=2)


def test_empty_table_is_skipped() -> None:
    decision = compute_cutoff(BRONZE, None, require_gold=False, gold_watermark=None)

    assert decision.cutoff is None
    assert decision.skipped_reason == "empty table"


def test_gold_gate_caps_cutoff_at_watermark() -> None:
    watermark = NEWEST - timedelta(hours=20)

    decision = compute_cutoff(
        BRONZE, NEWEST, require_gold=True, gold_watermark=watermark
    )

    assert decision.cutoff == watermark
    assert decision.gated_by_gold is True


def test_gold_gate_keeps_retention_cutoff_when_watermark_is_newer() -> None:
    decision = compute_cutoff(
        BRONZE, NEWEST, require_gold=True, gold_watermark=NEWEST
    )

    assert decision.cutoff == NEWEST - timedelta(hours=12)


def test_missing_gold_watermark_skips_gated_tables() -> None:
    decision = compute_cutoff(BRONZE, NEWEST, require_gold=True, gold_watermark=None)

    assert decision.cutoff is None
    assert decision.skipped_reason == "gold watermark missing"
    assert decision.gated_by_gold is True


def test_gold_gate_does_not_apply_to_flights_current() -> None:
    decision = compute_cutoff(CURRENT, NEWEST, require_gold=True, gold_watermark=None)

    assert decision.cutoff == NEWEST - timedelta(hours=2)
    assert decision.gated_by_gold is False


def test_gold_gate_disabled_ignores_watermark() -> None:
    decision = compute_cutoff(
        BRONZE,
        NEWEST,
        require_gold=False,
        gold_watermark=NEWEST - timedelta(hours=40),
    )

    assert decision.cutoff == NEWEST - timedelta(hours=12)
    assert decision.gated_by_gold is False


def test_default_policies_cover_only_swim_tables() -> None:
    policies = {p.table_name: p for p in default_policies()}

    assert set(policies) == {
        "users.nfs_demo.nfs_swim_flight_events_raw",
        "users.nfs_demo.nfs_flight_position_events",
        "users.nfs_demo.nfs_flights_current",
        "users.nfs_demo.nfs_flight_aircraft",
        "users.nfs_demo.nfs_flight_planned_routes",
    }
    assert all("notam" not in name and "nms" not in name for name in policies)
    raw = policies["users.nfs_demo.nfs_swim_flight_events_raw"]
    assert raw.keep_hours == 12
