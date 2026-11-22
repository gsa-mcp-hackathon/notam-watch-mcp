"""FAA NASR airport reference data (APT_BASE) for NOTAM location anchors.

The NASR 28-day subscription is public. Each AIRAC cycle publishes an APT CSV
zip whose APT_BASE.csv holds one row per landing facility (airport, heliport,
seaplane base, ...) with its FAA location identifier, optional ICAO id, and
airport reference point. NOTAM ``location`` values use these identifiers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests

from nfs_ingest.config import settings
from nfs_ingest.services.nms_state import _qualified_identifier

# Any known AIRAC effective date; cycles repeat every 28 days.
_AIRAC_REFERENCE = date(2024, 1, 25)
_AIRAC_DAYS = 28


def airac_effective_date(on: date | None = None) -> date:
    """Effective date of the AIRAC/NASR cycle in force on ``on`` (UTC today)."""
    target = on or datetime.now(UTC).date()
    cycles = (target - _AIRAC_REFERENCE).days // _AIRAC_DAYS
    return _AIRAC_REFERENCE + timedelta(days=cycles * _AIRAC_DAYS)


def apt_csv_url(effective: date) -> str:
    return (
        f"{settings.nasr_base_url.rstrip('/')}/extra/"
        f"{effective:%d_%b_%Y}_APT_CSV.zip"
    )


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_apt_base(
    archive: bytes,
    *,
    loaded_at: datetime,
) -> list[dict[str, Any]]:
    """Normalize APT_BASE.csv rows from an NASR APT CSV zip."""
    source_sha256 = hashlib.sha256(archive).hexdigest()
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        name = next(
            (n for n in bundle.namelist() if n.upper().endswith("APT_BASE.CSV")),
            None,
        )
        if name is None:
            raise ValueError("NASR APT archive does not contain APT_BASE.csv.")
        text = io.TextIOWrapper(bundle.open(name), encoding="latin-1", newline="")
        rows: list[dict[str, Any]] = []
        for raw in csv.DictReader(text):
            site_no = _text(raw.get("SITE_NO"))
            arpt_id = _text(raw.get("ARPT_ID"))
            latitude = _float(raw.get("LAT_DECIMAL"))
            longitude = _float(raw.get("LONG_DECIMAL"))
            if not site_no or not arpt_id:
                continue
            if latitude is not None and not -90 <= latitude <= 90:
                latitude = None
            if longitude is not None and not -180 <= longitude <= 180:
                longitude = None
            effective = _text(raw.get("EFF_DATE"))
            rows.append(
                {
                    "site_no": site_no,
                    "arpt_id": arpt_id.upper(),
                    "icao_id": (_text(raw.get("ICAO_ID")) or "").upper() or None,
                    "arpt_name": _text(raw.get("ARPT_NAME")),
                    "city": _text(raw.get("CITY")),
                    "state_code": _text(raw.get("STATE_CODE")),
                    "country_code": _text(raw.get("COUNTRY_CODE")),
                    "site_type_code": _text(raw.get("SITE_TYPE_CODE")),
                    "ownership_type_code": _text(raw.get("OWNERSHIP_TYPE_CODE")),
                    "facility_use_code": _text(raw.get("FACILITY_USE_CODE")),
                    "arpt_status": _text(raw.get("ARPT_STATUS")),
                    "resp_artcc_id": _text(raw.get("RESP_ARTCC_ID")),
                    "latitude": latitude,
                    "longitude": longitude,
                    "elevation_ft": _float(raw.get("ELEV")),
                    "nasr_effective_date": (
                        effective.replace("/", "-") if effective else None
                    ),
                    "source_file": name,
                    "source_sha256": source_sha256,
                    "loaded_at": loaded_at.isoformat(),
                }
            )
    return rows


def download_apt_archive(
    effective: date,
    session: requests.Session | None = None,
) -> bytes:
    response = (session or requests.Session()).get(
        apt_csv_url(effective),
        timeout=settings.nasr_timeout_seconds,
    )
    response.raise_for_status()
    if response.content[:2] != b"PK":
        raise ValueError("NASR APT download was not a zip archive.")
    return response.content


def build_airport_code_lookup(
    rows: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Index facilities by FAA location id and ICAO id for NOTAM anchors."""
    lookup: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("latitude") is None or row.get("longitude") is None:
            continue
        entry = {
            "arpt_id": row.get("arpt_id"),
            "icao_id": row.get("icao_id"),
            "arpt_name": row.get("arpt_name"),
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "elevation_ft": row.get("elevation_ft"),
        }
        for code in {row.get("arpt_id"), row.get("icao_id")}:
            if code:
                lookup.setdefault(str(code).upper(), []).append(entry)
    return lookup


def _json_chunks(
    rows: Sequence[dict[str, Any]],
    batch_size: int,
    max_chars: int | None = None,
) -> Iterable[str]:
    """Serialize rows as JSON arrays under the SQL parameter size limit."""
    limit = max_chars or settings.databricks_max_parameter_chars
    parts: list[str] = []
    total = 2
    for row in rows:
        part = json.dumps(row, separators=(",", ":"))
        if parts and (len(parts) >= batch_size or total + len(part) + 1 > limit):
            yield "[" + ",".join(parts) + "]"
            parts, total = [], 2
        parts.append(part)
        total += len(part) + 1
    if parts:
        yield "[" + ",".join(parts) + "]"


_AIRPORT_SCHEMA = (
    "ARRAY<STRUCT<site_no:STRING,arpt_id:STRING,icao_id:STRING,arpt_name:STRING,"
    "city:STRING,state_code:STRING,country_code:STRING,site_type_code:STRING,"
    "ownership_type_code:STRING,facility_use_code:STRING,arpt_status:STRING,"
    "resp_artcc_id:STRING,latitude:DOUBLE,longitude:DOUBLE,elevation_ft:DOUBLE,"
    "nasr_effective_date:STRING,source_file:STRING,source_sha256:STRING,"
    "loaded_at:STRING>>"
)


class NasrAirportStore:
    """Persist and read the NASR airport reference table via a SQL warehouse."""

    def __init__(
        self,
        *,
        warehouse_id: str | None = None,
        profile: str | None = None,
        client: Any | None = None,
        table_name: str | None = None,
    ) -> None:
        self.warehouse_id = warehouse_id or settings.databricks_warehouse_id
        if not self.warehouse_id:
            raise ValueError("A Databricks SQL warehouse ID is required.")
        if client is None:
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient(
                profile=profile or settings.databricks_profile or None
            )
        self.client = client
        self.table = _qualified_identifier(table_name or settings.nasr_airports_table)

    def ensure_table(self) -> None:
        self._execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
              site_no STRING NOT NULL,
              arpt_id STRING NOT NULL,
              icao_id STRING,
              arpt_name STRING,
              city STRING,
              state_code STRING,
              country_code STRING,
              site_type_code STRING,
              ownership_type_code STRING,
              facility_use_code STRING,
              arpt_status STRING,
              resp_artcc_id STRING,
              latitude DOUBLE,
              longitude DOUBLE,
              elevation_ft DOUBLE,
              h3_r7 BIGINT,
              nasr_effective_date DATE,
              source_file STRING,
              source_sha256 STRING,
              loaded_at TIMESTAMP
            ) USING DELTA
            COMMENT 'FAA NASR APT_BASE landing facilities (28-day cycle).'
            """
        )

    def replace_cycle(
        self,
        rows: Sequence[dict[str, Any]],
        batch_size: int = 5000,
    ) -> dict[str, Any]:
        """Upsert one NASR cycle, then drop facilities not in it.

        The key is (SITE_NO, ARPT_ID): NASR occasionally reuses a SITE_NO for
        two facilities (e.g. 23747.31 is both airport 9XS4 and heliport 16XA).
        """
        if not rows:
            raise ValueError("Refusing to replace airports with an empty cycle.")
        rows = list({(row["site_no"], row["arpt_id"]): row for row in rows}.values())
        effective = rows[0]["nasr_effective_date"]
        for chunk in _json_chunks(rows, batch_size):
            self._execute(
                f"""
                MERGE INTO {self.table} AS target
                USING (
                  SELECT
                    row.site_no, row.arpt_id, row.icao_id, row.arpt_name,
                    row.city, row.state_code, row.country_code,
                    row.site_type_code, row.ownership_type_code,
                    row.facility_use_code, row.arpt_status, row.resp_artcc_id,
                    row.latitude, row.longitude, row.elevation_ft,
                    CASE
                      WHEN row.latitude IS NOT NULL AND row.longitude IS NOT NULL
                      THEN h3_longlatash3(row.longitude, row.latitude, 7)
                    END AS h3_r7,
                    try_cast(row.nasr_effective_date AS DATE) AS nasr_effective_date,
                    row.source_file, row.source_sha256,
                    try_cast(row.loaded_at AS TIMESTAMP) AS loaded_at
                  FROM explode(from_json(:rows_json, '{_AIRPORT_SCHEMA}')) AS s(row)
                ) AS source
                ON target.site_no = source.site_no
                  AND target.arpt_id = source.arpt_id
                WHEN MATCHED THEN UPDATE SET *
                WHEN NOT MATCHED THEN INSERT *
                """,
                rows_json=chunk,
            )
        self._execute(
            f"DELETE FROM {self.table} "
            "WHERE nasr_effective_date <> try_cast(:effective AS DATE)",
            effective=effective,
        )
        response = self._execute(
            f"SELECT count(*) FROM {self.table} "
            "WHERE nasr_effective_date = try_cast(:effective AS DATE)",
            effective=effective,
        )
        stored = int(response.result.data_array[0][0]) if response.result else 0
        if stored != len(rows):
            raise RuntimeError("NASR airport verification count did not match input.")
        return {
            "table": settings.nasr_airports_table,
            "nasr_effective_date": effective,
            "airports": stored,
            "verified": True,
        }

    def load_code_lookup(self) -> dict[str, list[dict[str, Any]]]:
        """Read the stored cycle back as a NOTAM location-code lookup."""
        response = self._execute(
            f"""
            SELECT arpt_id, icao_id, arpt_name, latitude, longitude, elevation_ft
            FROM {self.table}
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            """
        )
        columns = (
            "arpt_id",
            "icao_id",
            "arpt_name",
            "latitude",
            "longitude",
            "elevation_ft",
        )
        rows: list[dict[str, Any]] = []
        chunk = response.result
        while chunk is not None:
            for values in chunk.data_array or []:
                row = dict(zip(columns, values, strict=False))
                for key in ("latitude", "longitude", "elevation_ft"):
                    row[key] = _float(row[key])
                rows.append(row)
            if not chunk.next_chunk_index:
                break
            chunk = self.client.statement_execution.get_statement_result_chunk_n(
                response.statement_id, chunk.next_chunk_index
            )
        return build_airport_code_lookup(rows)

    def _execute(self, statement: str, **parameters: str) -> Any:
        from databricks.sdk.service.sql import (
            StatementParameterListItem,
            StatementState,
        )

        response = self.client.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=self.warehouse_id,
            parameters=[
                StatementParameterListItem(name=name, value=value, type="STRING")
                for name, value in parameters.items()
            ]
            or None,
            wait_timeout="50s",
        )
        while response.status.state in {
            StatementState.PENDING,
            StatementState.RUNNING,
        }:
            response = self.client.statement_execution.get_statement(
                response.statement_id
            )
        if response.status.state != StatementState.SUCCEEDED:
            detail = (
                response.status.error.message if response.status.error else "unknown"
            )
            raise RuntimeError(f"Databricks SQL statement failed: {detail}")
        return response
