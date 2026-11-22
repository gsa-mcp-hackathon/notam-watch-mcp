"""Read-only FAA NMS API connectivity and ingestion helpers."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import requests
from requests import Session

from nfs_ingest.config import settings
from nfs_ingest.services.nms_parser import PreparedNmsRecord, prepare_nms_payload
from nfs_ingest.services.nms_watermark import NmsSyncPlan, plan_sync

NMS_CLASSIFICATIONS = (
    "INTERNATIONAL",
    "MILITARY",
    "LOCAL_MILITARY",
    "DOMESTIC",
    "FDC",
)


class NmsConfigurationError(ValueError):
    """Raised when required NMS settings are missing."""


class NmsInitialLoadRequired(RuntimeError):
    """Raised when the compressed AIXM baseline must be loaded first."""


def _configured() -> bool:
    return bool(settings.nms_client_id and settings.nms_client_secret)


def _redact_error(error: Exception) -> str:
    message = str(error)
    for secret in (settings.nms_client_id, settings.nms_client_secret):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


class NmsClient:
    """Small client for OAuth and filtered NOTAM reads."""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session or requests.Session()

    def _require_configuration(self) -> None:
        if not _configured():
            raise NmsConfigurationError(
                "Missing NMS_CLIENT_ID or NMS_CLIENT_SECRET."
            )

    def get_access_token(self) -> str:
        self._require_configuration()
        response = self.session.post(
            settings.nms_auth_url,
            data={"grant_type": "client_credentials"},
            auth=(settings.nms_client_id, settings.nms_client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=settings.nms_timeout_seconds,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise ValueError("NMS auth response did not include access_token.")
        return str(token)

    def get_notams_since(self, since: datetime) -> tuple[dict[str, Any], str | None]:
        token = self.get_access_token()
        normalized_since = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self.session.get(
            f"{settings.nms_api_base_url.rstrip('/')}/notams",
            params={"lastUpdatedDate": normalized_since},
            headers={
                "Authorization": f"Bearer {token}",
                "nmsResponseFormat": "GEOJSON",
            },
            timeout=settings.nms_timeout_seconds,
        )
        response.raise_for_status()
        return response.json(), response.headers.get("x-request-id")

    def get_classification_baseline(
        self,
        classification: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Download the complete active GeoJSON set for one classification.

        Per NMS-API 1.0.17, ``classification`` as the sole query parameter
        returns a relative content path to a (gzip) file containing every
        active NOTAM for that classification. The path expires after five
        minutes, so it is downloaded immediately with the same bearer token.
        """
        normalized = classification.strip().upper()
        if normalized not in NMS_CLASSIFICATIONS:
            raise ValueError(
                f"Unsupported NMS classification {classification!r}; expected one "
                f"of {', '.join(NMS_CLASSIFICATIONS)}."
            )
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "nmsResponseFormat": "GEOJSON",
        }
        response = self.session.get(
            f"{settings.nms_api_base_url.rstrip('/')}/notams",
            params={"classification": normalized, "allowRedirect": "false"},
            headers=headers,
            timeout=settings.nms_timeout_seconds,
        )
        response.raise_for_status()
        request_id = response.headers.get("x-request-id")
        content_path = ((response.json() or {}).get("data") or {}).get("url")
        if not content_path:
            raise ValueError(
                "NMS classification response did not include data.url."
            )
        content = self.session.get(
            _absolute_content_url(content_path),
            headers={"Authorization": f"Bearer {token}"},
            timeout=settings.nms_content_timeout_seconds,
        )
        content.raise_for_status()
        return _decode_feature_file(content.content), request_id


def _absolute_content_url(content_path: str) -> str:
    if content_path.startswith(("http://", "https://")):
        return content_path
    base = urlsplit(settings.nms_api_base_url)
    return f"{base.scheme}://{base.netloc}/{content_path.lstrip('/')}"


def _decode_feature_file(raw: bytes) -> list[dict[str, Any]]:
    """Decode a (possibly gzip) GeoJSON baseline into a feature list."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    document = json.loads(raw)
    if isinstance(document, list):
        features = document
    elif isinstance(document, dict):
        features = (
            document.get("features")
            or (document.get("data") or {}).get("geojson")
            or []
        )
    else:
        features = []
    if not isinstance(features, list):
        raise ValueError("NMS baseline file did not contain a feature array.")
    return [feature for feature in features if isinstance(feature, dict)]


def _json_chars(row: dict[str, Any] | None) -> int:
    if row is None:
        return 0
    return len(json.dumps(row, default=str, separators=(",", ":"))) + 1


def _batches(
    records: Sequence[PreparedNmsRecord],
    size: int,
    max_chars: int | None = None,
) -> Iterable[Sequence[PreparedNmsRecord]]:
    """Split records by count AND by serialized size.

    The Databricks SQL Statement Execution API limits the combined size of a
    statement's parameters (1,048,576 characters). Each MERGE receives one
    table's rows as a single JSON parameter, so the running Bronze, version,
    and hazard payload sizes are each kept under ``max_chars``.
    """
    step = max(1, size)
    limit = max_chars or settings.databricks_max_parameter_chars
    batch: list[PreparedNmsRecord] = []
    totals = [2, 2, 2]
    for record in records:
        sizes = (
            _json_chars(record.bronze),
            _json_chars(record.version),
            _json_chars(record.hazard),
        )
        if any(value + 2 > limit for value in sizes):
            raise ValueError(
                f"NMS record {record.version.get('nms_id')} exceeds the "
                f"{limit} character SQL parameter limit on its own."
            )
        if batch and (
            len(batch) >= step
            or any(total + value > limit for total, value in zip(totals, sizes))
        ):
            yield batch
            batch, totals = [], [2, 2, 2]
        batch.append(record)
        totals = [total + value for total, value in zip(totals, sizes)]
    if batch:
        yield batch


def _write_in_batches(
    writer: Any,
    records: Sequence[PreparedNmsRecord],
    batch_size: int | None = None,
) -> dict[str, Any]:
    """Write records in bounded MERGE batches and sum the verified counts."""
    totals = {"bronze_records": 0, "notam_versions": 0, "hazard_volumes": 0}
    batches = 0
    for batch in _batches(records, batch_size or settings.nms_write_batch_size):
        storage = writer.write(list(batch))
        batches += 1
        for key in totals:
            totals[key] += int(storage.get(key) or 0)
    return {**totals, "batches": batches, "verified": True}


def _parse_summary(records: Sequence[PreparedNmsRecord]) -> dict[str, Any]:
    sources = Counter(
        (record.hazard or {}).get("geometry_source") or "NO_USABLE_GEOMETRY"
        for record in records
    )
    anchors = Counter(
        record.version.get("anchor_source") or "NONE" for record in records
    )
    return {
        "records": len(records),
        "with_hazard_geometry": sum(1 for record in records if record.hazard),
        "geometry_sources": dict(sources.most_common()),
        "anchor_sources": dict(anchors.most_common()),
    }


def probe_connection(client: NmsClient | None = None) -> dict[str, Any]:
    """Authenticate and perform a narrow, read-only NOTAM delta request."""
    checked_at = datetime.now(UTC)
    if not _configured():
        return {
            "provider": "faa-nms",
            "state": "configuration-error",
            "connected": False,
            "checked_at": checked_at.isoformat(),
            "detail": "Missing NMS_CLIENT_ID or NMS_CLIENT_SECRET.",
        }
    since = checked_at - timedelta(minutes=settings.nms_probe_window_minutes)
    try:
        payload, request_id = (client or NmsClient()).get_notams_since(since)
        data = payload.get("data") or {}
        notams = data.get("geojson") or data.get("aixm") or []
        return {
            "provider": "faa-nms",
            "state": "connected",
            "connected": True,
            "checked_at": checked_at.isoformat(),
            "query_since": since.isoformat(),
            "response_status": payload.get("status"),
            "records_returned": len(notams) if isinstance(notams, list) else None,
            "request_id": request_id,
        }
    except Exception as exc:
        return {
            "provider": "faa-nms",
            "state": "connection-error",
            "connected": False,
            "checked_at": checked_at.isoformat(),
            "detail": _redact_error(exc),
        }


def ingest_incremental(
    *,
    writer: Any,
    watermark_store: Any,
    client: NmsClient | None = None,
    since: datetime | None = None,
    advance_watermark: bool = True,
    now: datetime | None = None,
    airport_code_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Fetch, parse, persist, verify, then optionally advance the watermark."""
    run_started_at = now or datetime.now(UTC)
    if run_started_at.tzinfo is None:
        raise ValueError("NMS run timestamps must include a timezone.")
    run_started_at = run_started_at.astimezone(UTC)
    last_successful = watermark_store.get_last_successful_pull_at()
    if since is None:
        plan = plan_sync(last_successful, now=run_started_at)
        if plan.mode == "initial":
            raise NmsInitialLoadRequired(
                "NMS initial load is required before watermark advancement. "
                "Run the ingestion job with --mode initial to load the "
                "classification baseline first."
            )
    else:
        if since.tzinfo is None:
            raise ValueError("Explicit NMS --since timestamps must include a timezone.")
        query_since = since.astimezone(UTC)
        if query_since > run_started_at:
            raise ValueError("Explicit NMS --since cannot be in the future.")
        plan = NmsSyncPlan(
            mode="incremental",
            run_started_at=run_started_at,
            last_successful_pull_at=last_successful,
            query_since=query_since,
            commit_watermark_at=run_started_at,
            reason="Explicit incremental start supplied by the operator.",
        )

    watermark_store.mark_started(plan)
    try:
        payload, request_id = (client or NmsClient()).get_notams_since(
            plan.query_since
        )
        records = prepare_nms_payload(
            payload,
            fetched_at=run_started_at,
            request_mode=plan.mode,
            request_watermark=plan.query_since,
            airport_code_lookup=airport_code_lookup,
        )
        storage = _write_in_batches(writer, records)
        watermark_store.mark_succeeded(
            plan,
            request_id=request_id,
            records_written=storage["bronze_records"],
            advance_watermark=advance_watermark,
        )
    except Exception as exc:
        watermark_store.mark_failed(plan, error_detail=_redact_error(exc))
        raise

    return {
        "provider": "faa-nms",
        "state": "succeeded" if advance_watermark else "shadow-succeeded",
        "request_id": request_id,
        "query_since": plan.query_since.isoformat(),
        "watermark_advanced": advance_watermark,
        **storage,
    }


def ingest_initial(
    *,
    writer: Any,
    watermark_store: Any,
    client: NmsClient | None = None,
    classifications: Sequence[str] | None = None,
    advance_watermark: bool = True,
    now: datetime | None = None,
    batch_size: int | None = None,
    airport_code_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Load the active classification baseline(s), then optionally commit.

    Every requested classification must download, parse, and persist (with
    write verification) before the watermark advances to the run start time.
    Incremental pulls then catch up from that point within the 24 hour NMS
    lookback. A failure in any classification marks the run failed and leaves
    the previous watermark untouched; MERGEs are idempotent so a rerun is safe.
    """
    run_started_at = now or datetime.now(UTC)
    if run_started_at.tzinfo is None:
        raise ValueError("NMS run timestamps must include a timezone.")
    run_started_at = run_started_at.astimezone(UTC)
    selected = [
        value.strip().upper()
        for value in (
            classifications
            or settings.nms_baseline_classifications.split(",")
        )
        if value and value.strip()
    ]
    if not selected:
        raise ValueError("At least one NMS classification is required.")
    unsupported = sorted(set(selected) - set(NMS_CLASSIFICATIONS))
    if unsupported:
        raise ValueError(
            f"Unsupported NMS classifications: {', '.join(unsupported)}"
        )

    plan = NmsSyncPlan(
        mode="initial",
        run_started_at=run_started_at,
        last_successful_pull_at=watermark_store.get_last_successful_pull_at(),
        query_since=None,
        commit_watermark_at=run_started_at,
        reason=f"Classification baseline load: {', '.join(selected)}.",
    )
    nms_client = client or NmsClient()
    watermark_store.mark_started(plan)
    request_ids: list[str] = []
    per_classification: dict[str, Any] = {}
    totals = {"bronze_records": 0, "notam_versions": 0, "hazard_volumes": 0}
    try:
        for classification in selected:
            features, request_id = nms_client.get_classification_baseline(
                classification
            )
            if request_id:
                request_ids.append(request_id)
            records = prepare_nms_payload(
                {"data": {"geojson": features}},
                fetched_at=run_started_at,
                request_mode="initial",
                request_watermark=None,
                airport_code_lookup=airport_code_lookup,
            )
            storage = _write_in_batches(writer, records, batch_size)
            for key in totals:
                totals[key] += storage[key]
            per_classification[classification] = {
                "features_downloaded": len(features),
                **_parse_summary(records),
                **storage,
            }
        watermark_store.mark_succeeded(
            plan,
            request_id=",".join(request_ids)[:2000] or None,
            records_written=totals["bronze_records"],
            advance_watermark=advance_watermark,
        )
    except Exception as exc:
        watermark_store.mark_failed(plan, error_detail=_redact_error(exc))
        raise

    return {
        "provider": "faa-nms",
        "mode": "initial",
        "state": "succeeded" if advance_watermark else "shadow-succeeded",
        "classifications": per_classification,
        "request_ids": request_ids,
        "watermark_advanced": advance_watermark,
        "commit_watermark_at": run_started_at.isoformat(),
        **totals,
        "verified": True,
    }

