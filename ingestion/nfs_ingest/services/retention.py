"""Bounded-storage retention for SWIM Bronze/Silver tables.

Each policy expires rows older than a cutoff computed from that table's own
newest timestamp (not wall-clock), so a paused or stalled feed never wipes the
last data it received. Optionally, Bronze and positions are only trimmed up to
the gold analytics watermark, so nothing is dropped before it reaches gold.
NMS/NOTAM tables are intentionally out of scope.

run_retention defaults to dry_run=True (count + audit only). The purge step
(_purge) is intentionally left for the table owner to enable; until then a
non-dry run fails loudly instead of silently skipping.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nfs_ingest.config import settings
from nfs_ingest.services.nms_state import _qualified_identifier


@dataclass(frozen=True)
class RetentionPolicy:
    table_name: str
    time_column: str
    keep_hours: float
    gold_gated: bool


@dataclass(frozen=True)
class RetentionDecision:
    table_name: str
    time_column: str
    max_timestamp: datetime | None
    cutoff: datetime | None
    gated_by_gold: bool
    skipped_reason: str | None = None


def default_policies() -> list[RetentionPolicy]:
    """SWIM tables only; NMS/NOTAM tables are never included."""
    return [
        RetentionPolicy(
            settings.swim_bronze_table,
            "received_at",
            settings.swim_bronze_retention_hours,
            gold_gated=True,
        ),
        RetentionPolicy(
            settings.swim_positions_table,
            "received_at",
            settings.swim_positions_retention_hours,
            gold_gated=True,
        ),
        RetentionPolicy(
            settings.swim_flights_current_table,
            "observed_at",
            settings.flights_current_stale_hours,
            gold_gated=False,
        ),
        # Aircraft identity must outlive the positions it describes.
        RetentionPolicy(
            settings.swim_flight_aircraft_table,
            "received_at",
            settings.swim_positions_retention_hours,
            gold_gated=True,
        ),
        RetentionPolicy(
            settings.swim_planned_routes_table,
            "received_at",
            settings.swim_positions_retention_hours,
            gold_gated=True,
        ),
    ]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def compute_cutoff(
    policy: RetentionPolicy,
    max_timestamp: datetime | None,
    *,
    require_gold: bool,
    gold_watermark: datetime | None,
) -> RetentionDecision:
    """Cutoff = MAX(ts) - keep_hours, optionally capped at the gold watermark."""
    newest = _utc(max_timestamp)
    gated = bool(require_gold and policy.gold_gated)
    if newest is None:
        return RetentionDecision(
            policy.table_name, policy.time_column, None, None, gated, "empty table"
        )
    cutoff = newest - timedelta(hours=float(policy.keep_hours))
    if gated:
        watermark = _utc(gold_watermark)
        if watermark is None:
            return RetentionDecision(
                policy.table_name,
                policy.time_column,
                newest,
                None,
                gated,
                "gold watermark missing",
            )
        cutoff = min(cutoff, watermark)
    return RetentionDecision(
        policy.table_name, policy.time_column, newest, cutoff, gated
    )


def _timestamp_literal(value: datetime) -> str:
    return "TIMESTAMP '" + value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") + "'"


def _string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _gold_watermark(spark: Any) -> datetime | None:
    table = _qualified_identifier(settings.nms_watermark_table)
    rows = spark.sql(
        f"SELECT last_successful_pull_at FROM {table} "
        f"WHERE feed_name = {_string_literal(settings.gold_watermark_feed)}"
    ).collect()
    return rows[0][0] if rows else None


def _ensure_audit_table(spark: Any, audit: str) -> None:
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {audit} (
          run_at TIMESTAMP NOT NULL,
          table_name STRING NOT NULL,
          time_column STRING NOT NULL,
          cutoff TIMESTAMP,
          rows_deleted BIGINT NOT NULL,
          dry_run BOOLEAN NOT NULL,
          gated_by_gold BOOLEAN NOT NULL
        ) USING DELTA
        COMMENT 'NFS SWIM retention runs (rows_deleted = eligible rows on dry runs).'
        """
    )


def _purge(spark: Any, table: str, predicate: str) -> None:
    """Expire rows matching ``predicate`` from ``table`` and reclaim files.

    Left for the table owner to enable (remove expired rows, then reclaim
    files under the table's 24 h deletedFileRetentionDuration).
    """
    raise NotImplementedError(
        f"Retention purge is not enabled for {table}; run with --dry-run."
    )


def run_retention(
    spark: Any,
    dry_run: bool = True,
    policies: list[RetentionPolicy] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Count eligible rows per policy, purge them when dry_run is False, audit."""
    run_at = now or datetime.now(UTC)
    require_gold = settings.retention_require_gold_watermark
    gold = _gold_watermark(spark) if require_gold else None
    audit = _qualified_identifier(settings.retention_audit_table)
    _ensure_audit_table(spark, audit)

    results: list[dict[str, Any]] = []
    for policy in policies or default_policies():
        table = _qualified_identifier(policy.table_name)
        column = f"`{policy.time_column}`"
        newest = spark.sql(f"SELECT max({column}) FROM {table}").collect()[0][0]
        decision = compute_cutoff(
            policy, newest, require_gold=require_gold, gold_watermark=gold
        )
        result: dict[str, Any] = {
            **asdict(decision),
            "keep_hours": policy.keep_hours,
            "dry_run": dry_run,
            "rows_eligible": 0,
            "rows_deleted": 0,
            "vacuumed": False,
        }
        cutoff_sql = "NULL"
        if decision.cutoff is not None:
            cutoff_sql = _timestamp_literal(decision.cutoff)
            predicate = f"{column} < {cutoff_sql}"
            eligible = spark.sql(
                f"SELECT count(*) FROM {table} WHERE {predicate}"
            ).collect()[0][0]
            result["rows_eligible"] = int(eligible)
            if not dry_run:
                _purge(spark, table, predicate)
                result["rows_deleted"] = int(eligible)
                result["vacuumed"] = True
        audited_rows = result["rows_eligible"] if dry_run else result["rows_deleted"]
        spark.sql(
            f"""
            INSERT INTO {audit} VALUES (
              {_timestamp_literal(run_at)},
              {_string_literal(policy.table_name)},
              {_string_literal(policy.time_column)},
              {cutoff_sql},
              {audited_rows},
              {str(dry_run).upper()},
              {str(decision.gated_by_gold).upper()}
            )
            """
        )
        for key in ("max_timestamp", "cutoff"):
            if result[key] is not None:
                result[key] = result[key].isoformat()
        results.append(result)

    return {
        "operation": "swim-retention",
        "run_at": run_at.isoformat(),
        "dry_run": dry_run,
        "require_gold_watermark": require_gold,
        "gold_watermark": _utc(gold).isoformat() if gold else None,
        "tables": results,
    }
