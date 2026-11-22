"""Plan gap-safe NMS pulls from a durable successful-run watermark."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from nfs_ingest.config import settings


@dataclass(frozen=True)
class NmsSyncPlan:
    """One NMS pull decision derived from the last committed watermark."""

    mode: Literal["initial", "incremental"]
    run_started_at: datetime
    last_successful_pull_at: datetime | None
    query_since: datetime | None
    commit_watermark_at: datetime
    reason: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in (
            "run_started_at",
            "last_successful_pull_at",
            "query_since",
            "commit_watermark_at",
        ):
            value = payload[field]
            payload[field] = value.isoformat() if value is not None else None
        return payload


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("NMS watermark timestamps must include a timezone.")
    return value.astimezone(UTC)


def plan_sync(
    last_successful_pull_at: datetime | None,
    *,
    now: datetime | None = None,
) -> NmsSyncPlan:
    """Choose incremental catch-up or an initial-load rebaseline.

    The NMS incremental endpoint only permits a lastUpdatedDate within the
    configured maximum lookback. A successful run commits ``run_started_at``
    rather than completion time so records changing during the request are
    replayed on the next run instead of being skipped.
    """
    run_started_at = _as_utc(now or datetime.now(UTC))
    if last_successful_pull_at is None:
        return NmsSyncPlan(
            mode="initial",
            run_started_at=run_started_at,
            last_successful_pull_at=None,
            query_since=None,
            commit_watermark_at=run_started_at,
            reason="No successful watermark exists; establish a full baseline.",
        )

    watermark = _as_utc(last_successful_pull_at)
    if watermark > run_started_at:
        raise ValueError("NMS watermark cannot be later than the run start time.")

    max_lookback = timedelta(hours=settings.nms_incremental_max_lookback_hours)
    gap = run_started_at - watermark
    if gap >= max_lookback:
        return NmsSyncPlan(
            mode="initial",
            run_started_at=run_started_at,
            last_successful_pull_at=watermark,
            query_since=None,
            commit_watermark_at=run_started_at,
            reason=(
                "The last successful pull is outside the NMS incremental "
                "lookback; rebuild the active baseline with an initial load."
            ),
        )

    overlap = timedelta(minutes=settings.nms_incremental_overlap_minutes)
    earliest_allowed = run_started_at - max_lookback
    query_since = max(watermark - overlap, earliest_allowed)
    return NmsSyncPlan(
        mode="incremental",
        run_started_at=run_started_at,
        last_successful_pull_at=watermark,
        query_since=query_since,
        commit_watermark_at=run_started_at,
        reason="Catch up from the last successful pull with a deduplication overlap.",
    )
