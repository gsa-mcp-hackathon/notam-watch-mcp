"""FAA SWIM TFM R14 streaming ingestion: Bronze messages -> Silver positions.

Messages are received from the durable Solace queue in micro-batches. Each
batch is written to Delta (Bronze raw XML, Silver position events, and the
latest-position-per-flight table) and only then acknowledged, so a failed
write leaves messages on the queue for redelivery. All writes are idempotent
(MERGE on deterministic ids), so redelivered messages are not duplicated.

The row preparation functions are pure Python and unit tested; the Spark
writer is used inside Databricks Jobs on serverless compute.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nfs_ingest.config import settings
from nfs_ingest.services.swim_parser import parse_tfm_xml_messages

SOURCE = "FAA_SWIM_TFM_R14"


@dataclass
class SwimBatch:
    bronze: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    aircraft: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp(value: Any, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _valid_position(latitude: Any, longitude: Any) -> bool:
    if latitude is None or longitude is None:
        return False
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    # 0,0 is the parser's "no position" artifact, not a real TFM position.
    return not (abs(lat) < 1e-9 and abs(lon) < 1e-9)


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def flight_key(record: dict[str, Any]) -> str:
    """Stable flight identity: GUFI, then TFMS flightRef, then callsign+pair."""
    if record.get("gufi"):
        return f"GUFI:{record['gufi']}"
    if record.get("flight_ref"):
        return f"FREF:{record['flight_ref']}"
    return "ACID:" + "|".join(
        str(record.get(key) or "").upper()
        for key in ("flight_id", "origin", "destination")
    )


def envelope_from_message(message: Any, received_at: datetime) -> dict[str, Any]:
    """Lossless Bronze envelope; a parse failure is recorded, never dropped."""
    payload_xml = message.get_payload_as_string()
    if payload_xml is None:
        payload_bytes = message.get_payload_as_bytes() or b""
        payload_xml = payload_bytes.decode("utf-8", errors="replace")
    parse_error = None
    try:
        parsed_records = parse_tfm_xml_messages(payload_xml) if payload_xml else []
    except Exception as exc:  # noqa: BLE001 - keep the raw message regardless
        parsed_records = []
        parse_error = f"{type(exc).__name__}: {exc}"[:500]
    try:
        properties = message.get_properties() or {}
    except Exception:  # noqa: BLE001
        properties = {}
    return {
        "received_at": received_at,
        "topic": message.get_destination_name(),
        "application_message_id": message.get_application_message_id(),
        "properties": properties,
        "payload_xml": payload_xml or "",
        "parsed_records": parsed_records,
        "parse_error": parse_error,
    }


def prepare_batch(envelopes: Sequence[dict[str, Any]]) -> SwimBatch:
    batch = SwimBatch()
    seen: set[str] = set()
    for envelope in envelopes:
        payload = envelope["payload_xml"]
        payload_hash = _sha256(payload)
        app_id = envelope.get("application_message_id") or ""
        bronze_id = _sha256(f"{app_id}:{payload_hash}")
        batch.stats["messages"] += 1
        if bronze_id in seen:
            batch.stats["duplicate_messages_in_batch"] += 1
            continue
        seen.add(bronze_id)
        received_at = envelope["received_at"]
        records = envelope.get("parsed_records") or []
        if envelope.get("parse_error"):
            batch.stats["parse_errors"] += 1
        batch.bronze.append(
            {
                "event_id": bronze_id,
                "received_at": received_at,
                "topic": envelope.get("topic") or "",
                "application_message_id": app_id,
                "payload_sha256": payload_hash,
                "payload_xml": payload,
                "properties_json": json.dumps(
                    envelope.get("properties") or {}, default=str
                ),
                # Planned routes go to their own table; keep Bronze lean.
                "parsed_records_json": json.dumps(
                    [
                        {k: v for k, v in r.items() if k != "planned_route"}
                        for r in records
                    ],
                    default=str,
                ),
                "parsed_record_count": len(records),
                "source": SOURCE,
                "parse_error": envelope.get("parse_error"),
            }
        )
        for index, record in enumerate(records):
            batch.stats["records"] += 1
            batch.stats[f"msg_type:{record.get('msg_type') or 'UNKNOWN'}"] += 1
            aircraft = _aircraft_row(record, received_at)
            if aircraft:
                batch.aircraft.append(aircraft)
                batch.stats["aircraft_type_records"] += 1
            route = _route_row(record, received_at)
            if route:
                batch.routes.append(route)
                batch.stats["planned_route_records"] += 1
            latitude = record.get("current_lat")
            longitude = record.get("current_lon")
            if not _valid_position(latitude, longitude):
                batch.stats["records_without_position"] += 1
                continue
            observed_at = _timestamp(record.get("event_time"), received_at)
            batch.positions.append(
                {
                    "event_id": _sha256(f"{bronze_id}:{index}"),
                    "bronze_event_id": bronze_id,
                    "record_index": index,
                    "flight_key": flight_key(record),
                    "flight_id": str(record.get("flight_id") or ""),
                    "gufi": record.get("gufi") or None,
                    "flight_ref": record.get("flight_ref") or None,
                    "msg_type": record.get("msg_type") or None,
                    "observed_at": observed_at,
                    "received_at": received_at,
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                    "altitude_ft": _positive(record.get("current_altitude_ft")),
                    "ground_speed_kts": _positive(record.get("velocity_kts")),
                    "track_heading_deg": None,
                    "vertical_rate_fpm": None,
                    "origin": record.get("origin") or None,
                    "destination": record.get("destination") or None,
                    "source": SOURCE,
                    "source_freshness_seconds": max(
                        0.0, (received_at - observed_at).total_seconds()
                    ),
                }
            )
            batch.stats["positions"] += 1
    return batch


_AIRCRAFT_FIELDS = (
    "aircraft_type",
    "aircraft_engine_class",
    "equipment_qualifier",
    "special_aircraft_qualifier",
    "aircraft_category",
    "user_category",
)


def _aircraft_row(
    record: dict[str, Any], received_at: datetime
) -> dict[str, Any] | None:
    """Aircraft identity for a flight, keyed by TFMS flightRef.

    Track reports carry flightRef but no type, so the type from flight-plan
    messages is joined to positions through flight_ref. Only records with a
    type are emitted (track reports also carry aircraftCategory, which would
    otherwise create one redundant row per position).
    """
    flight_ref = record.get("flight_ref")
    if not flight_ref or not record.get("aircraft_type"):
        return None
    return {
        "flight_ref": str(flight_ref),
        "gufi": record.get("gufi") or None,
        "flight_id": str(record.get("flight_id") or ""),
        "origin": record.get("origin") or None,
        "destination": record.get("destination") or None,
        **{key: record.get(key) or None for key in _AIRCRAFT_FIELDS},
        "source_msg_type": record.get("msg_type") or None,
        "observed_at": _timestamp(record.get("event_time"), received_at),
        "received_at": received_at,
    }


_ROUTES_COMMENT = (
    "Latest TFMS planned route per flightRef (R14 flightTraversalData2). "
    "Waypoint elapsed_s is seconds after departure; no per-waypoint "
    "altitude. Plan, not flown path."
)


def _optional_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    marker = datetime.min.replace(tzinfo=UTC)
    parsed = _timestamp(value, marker)
    return None if parsed == marker else parsed


def _route_row(
    record: dict[str, Any], received_at: datetime
) -> dict[str, Any] | None:
    """Planned (TFMS-modeled) route for a flight, keyed by flightRef.

    Waypoint times are seconds after departure; absolute times need the
    actual/proposed ETD. This is the plan, not the flown path (positions).
    """
    route = record.get("planned_route")
    flight_ref = record.get("flight_ref")
    if not route or not flight_ref or len(route.get("waypoints") or []) < 2:
        return None
    waypoints = route["waypoints"]
    etd = route.get("etd") or {}
    eta = route.get("eta") or {}
    return {
        "flight_ref": str(flight_ref),
        "gufi": record.get("gufi") or None,
        "flight_id": str(record.get("flight_id") or ""),
        "origin": record.get("origin") or None,
        "destination": record.get("destination") or None,
        "source_msg_type": record.get("msg_type") or None,
        "observed_at": _timestamp(record.get("event_time"), received_at),
        "received_at": received_at,
        "route_string": route.get("route_string") or None,
        "departure_procedure": route.get("departure_procedure") or None,
        "arrival_procedure": route.get("arrival_procedure") or None,
        "etd_type": etd.get("type") or None,
        "etd_time": _optional_timestamp(etd.get("time")),
        "eta_type": eta.get("type") or None,
        "eta_time": _optional_timestamp(eta.get("time")),
        "planned_altitude_ft": route.get("planned_altitude_ft"),
        "filed_true_airspeed_kts": route.get("filed_true_airspeed_kts"),
        "waypoint_count": len(waypoints),
        "route_geojson": json.dumps(
            {
                "type": "LineString",
                "coordinates": [[w["lon"], w["lat"]] for w in waypoints],
            },
            separators=(",", ":"),
        ),
        "waypoints_json": json.dumps(waypoints, separators=(",", ":")),
        "fixes_json": json.dumps(route.get("fixes") or [], separators=(",", ":")),
        "sectors": list(route.get("sectors") or []),
        "centers": list(route.get("centers") or []),
        "airways": list(route.get("airways") or []),
    }


def stream_messages(
    write_batch: Callable[[list[dict[str, Any]]], dict[str, Any]],
    *,
    duration_seconds: float,
    flush_max_messages: int,
    flush_max_seconds: float,
    receive_timeout_ms: int = 1000,
    service_factory: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Receive, write, then acknowledge SWIM messages in micro-batches."""
    from nfs_ingest.services.swim import _build_service, _configured

    if not _configured():
        raise ValueError("Missing SWIM queue credentials.")
    from solace.messaging.resources.queue import Queue

    service = (service_factory or _build_service)()
    receiver = None
    totals: Counter = Counter()
    pending: list[tuple[Any, dict[str, Any]]] = []
    started = clock()
    last_flush = started

    def flush() -> None:
        nonlocal last_flush
        last_flush = clock()
        if not pending:
            return
        result = write_batch([envelope for _, envelope in pending])
        for message, _ in pending:
            receiver.ack(message)
        totals["batches"] += 1
        totals["acknowledged"] += len(pending)
        for key, value in (result.get("stats") or {}).items():
            totals[key] += value
        log(
            f"batch {totals['batches']}: {len(pending)} messages, "
            f"{result.get('positions_written', 0)} positions, "
            f"{round(clock() - started)}s"
        )
        pending.clear()

    try:
        service.connect()
        queue = Queue.durable_exclusive_queue(settings.swim_queue)
        receiver = service.create_persistent_message_receiver_builder().build(queue)
        receiver.start()
        while clock() - started < duration_seconds:
            message = receiver.receive_message(receive_timeout_ms)
            if message is not None:
                pending.append(
                    (message, envelope_from_message(message, datetime.now(UTC)))
                )
                totals["received"] += 1
            if len(pending) >= flush_max_messages or (
                pending and clock() - last_flush >= flush_max_seconds
            ):
                flush()
        flush()
        return {
            "provider": "faa-swim-r14",
            "state": "succeeded",
            "duration_seconds": round(clock() - started, 1),
            **dict(totals),
        }
    finally:
        if receiver is not None:
            try:
                receiver.terminate(0)
            except Exception:  # noqa: BLE001
                pass
        try:
            service.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _identifier(name: str) -> str:
    from nfs_ingest.services.nms_state import _qualified_identifier

    return _qualified_identifier(name)


class SwimSparkWriter:
    """Idempotent Delta writes for one SWIM micro-batch (Spark on Databricks)."""

    def __init__(self, spark: Any) -> None:
        self.spark = spark
        self.bronze = _identifier(settings.swim_bronze_table)
        self.positions = _identifier(settings.swim_positions_table)
        self.current = _identifier(settings.swim_flights_current_table)
        self.aircraft = _identifier(settings.swim_flight_aircraft_table)
        self.routes = _identifier(settings.swim_planned_routes_table)

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {f.name for f in self.spark.table(table).schema.fields}
        missing = [f"{n} {t}" for n, t in columns.items() if n not in existing]
        if missing:
            self.spark.sql(f"ALTER TABLE {table} ADD COLUMNS ({', '.join(missing)})")

    def ensure_tables(self) -> None:
        self.spark.sql(
            f"""
            CREATE TABLE IF NOT EXISTS {self.bronze} (
              event_id STRING NOT NULL,
              received_at TIMESTAMP NOT NULL,
              topic STRING,
              application_message_id STRING,
              payload_sha256 STRING NOT NULL,
              payload_xml STRING NOT NULL,
              properties_json STRING,
              parsed_records_json STRING,
              parsed_record_count INT NOT NULL,
              source STRING NOT NULL,
              parse_error STRING
            ) USING DELTA
            """
        )
        self._add_missing_columns(self.bronze, {"parse_error": "STRING"})
        self._add_missing_columns(
            self.positions, {"flight_ref": "STRING", "msg_type": "STRING"}
        )
        self._add_missing_columns(self.current, {"flight_ref": "STRING"})
        self.spark.sql(
            f"""
            CREATE TABLE IF NOT EXISTS {self.aircraft} (
              flight_ref STRING NOT NULL,
              gufi STRING,
              flight_id STRING NOT NULL,
              origin STRING,
              destination STRING,
              aircraft_type STRING,
              aircraft_engine_class STRING,
              equipment_qualifier STRING,
              special_aircraft_qualifier STRING,
              aircraft_category STRING,
              user_category STRING,
              source_msg_type STRING,
              observed_at TIMESTAMP NOT NULL,
              received_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP NOT NULL
            ) USING DELTA
            COMMENT 'Latest aircraft type/categories per TFMS flightRef (R14).'
            """
        )
        self.spark.sql(
            f"""
            CREATE TABLE IF NOT EXISTS {self.routes} (
              flight_ref STRING NOT NULL,
              gufi STRING,
              flight_id STRING NOT NULL,
              origin STRING,
              destination STRING,
              source_msg_type STRING,
              observed_at TIMESTAMP NOT NULL,
              received_at TIMESTAMP NOT NULL,
              route_string STRING,
              departure_procedure STRING,
              arrival_procedure STRING,
              etd_type STRING,
              etd_time TIMESTAMP,
              eta_type STRING,
              eta_time TIMESTAMP,
              planned_altitude_ft DOUBLE,
              filed_true_airspeed_kts DOUBLE,
              waypoint_count INT NOT NULL,
              route_geojson STRING NOT NULL,
              waypoints_json STRING NOT NULL,
              fixes_json STRING,
              sectors ARRAY<STRING>,
              centers ARRAY<STRING>,
              airways ARRAY<STRING>,
              first_observed_at TIMESTAMP NOT NULL,
              revision_count INT NOT NULL,
              updated_at TIMESTAMP NOT NULL
            ) USING DELTA
            COMMENT '{_ROUTES_COMMENT}'
            """
        )

    def write(self, envelopes: list[dict[str, Any]]) -> dict[str, Any]:
        from pyspark.sql import functions as F
        from pyspark.sql.window import Window

        batch = prepare_batch(envelopes)
        if batch.bronze:
            bronze_df = self.spark.createDataFrame(batch.bronze, _BRONZE_SCHEMA)
            bronze_df.createOrReplaceTempView("swim_bronze_batch")
            self.spark.sql(
                f"""
                MERGE INTO {self.bronze} AS t
                USING swim_bronze_batch AS s ON t.event_id = s.event_id
                WHEN NOT MATCHED THEN INSERT *
                """
            )
        if batch.positions:
            positions_df = self.spark.createDataFrame(
                batch.positions, _POSITION_SCHEMA
            ).withColumn("h3_r7", F.expr("h3_longlatash3(longitude, latitude, 7)"))
            positions_df.createOrReplaceTempView("swim_positions_batch")
            self.spark.sql(
                f"""
                MERGE INTO {self.positions} AS t
                USING (
                  SELECT event_id, bronze_event_id, record_index, flight_key,
                    flight_id, gufi, flight_ref, msg_type, observed_at,
                    received_at, latitude,
                    longitude, altitude_ft, ground_speed_kts, track_heading_deg,
                    vertical_rate_fpm, origin, destination, h3_r7, source,
                    source_freshness_seconds
                  FROM swim_positions_batch
                ) AS s ON t.event_id = s.event_id
                WHEN NOT MATCHED THEN INSERT *
                """
            )
            latest = (
                positions_df.withColumn(
                    "_rank",
                    F.row_number().over(
                        Window.partitionBy("flight_key").orderBy(
                            F.col("observed_at").desc(), F.col("event_id").desc()
                        )
                    ),
                )
                .filter("_rank = 1")
                .drop("_rank")
            )
            latest.createOrReplaceTempView("swim_latest_batch")
            self.spark.sql(
                f"""
                MERGE INTO {self.current} AS t
                USING (
                  SELECT flight_key, event_id, flight_id, gufi, flight_ref,
                    observed_at, received_at, latitude, longitude, altitude_ft,
                    ground_speed_kts, track_heading_deg, vertical_rate_fpm,
                    origin, destination, h3_r7,
                    current_timestamp() AS updated_at
                  FROM swim_latest_batch
                ) AS s ON t.flight_key = s.flight_key
                WHEN MATCHED AND s.observed_at >= t.observed_at THEN UPDATE SET *
                WHEN NOT MATCHED THEN INSERT *
                """
            )
        if batch.aircraft:
            aircraft_df = self.spark.createDataFrame(batch.aircraft, _AIRCRAFT_SCHEMA)
            aircraft_df.withColumn(
                "_rank",
                F.row_number().over(
                    Window.partitionBy("flight_ref").orderBy(
                        F.col("aircraft_type").isNull(),
                        F.col("observed_at").desc(),
                    )
                ),
            ).filter("_rank = 1").drop("_rank").createOrReplaceTempView(
                "swim_aircraft_batch"
            )
            # Newer messages win, but never erase a known type with a blank.
            self.spark.sql(
                f"""
                MERGE INTO {self.aircraft} AS t
                USING (
                  SELECT *, current_timestamp() AS updated_at
                  FROM swim_aircraft_batch
                ) AS s ON t.flight_ref = s.flight_ref
                WHEN MATCHED AND s.observed_at >= t.observed_at THEN UPDATE SET
                  gufi = coalesce(s.gufi, t.gufi),
                  flight_id = s.flight_id,
                  origin = coalesce(s.origin, t.origin),
                  destination = coalesce(s.destination, t.destination),
                  aircraft_type = coalesce(s.aircraft_type, t.aircraft_type),
                  aircraft_engine_class =
                    coalesce(s.aircraft_engine_class, t.aircraft_engine_class),
                  equipment_qualifier =
                    coalesce(s.equipment_qualifier, t.equipment_qualifier),
                  special_aircraft_qualifier = coalesce(
                    s.special_aircraft_qualifier, t.special_aircraft_qualifier
                  ),
                  aircraft_category =
                    coalesce(s.aircraft_category, t.aircraft_category),
                  user_category = coalesce(s.user_category, t.user_category),
                  source_msg_type = s.source_msg_type,
                  observed_at = s.observed_at,
                  received_at = s.received_at,
                  updated_at = s.updated_at
                WHEN MATCHED AND t.aircraft_type IS NULL
                  AND s.aircraft_type IS NOT NULL THEN UPDATE SET
                  aircraft_type = s.aircraft_type,
                  aircraft_engine_class =
                    coalesce(t.aircraft_engine_class, s.aircraft_engine_class),
                  equipment_qualifier =
                    coalesce(t.equipment_qualifier, s.equipment_qualifier),
                  updated_at = s.updated_at
                WHEN NOT MATCHED THEN INSERT *
                """
            )
        if batch.routes:
            routes_df = self.spark.createDataFrame(batch.routes, _ROUTE_SCHEMA)
            routes_df.withColumn(
                "_rank",
                F.row_number().over(
                    Window.partitionBy("flight_ref").orderBy(
                        F.col("observed_at").desc(), F.col("waypoint_count").desc()
                    )
                ),
            ).filter("_rank = 1").drop("_rank").createOrReplaceTempView(
                "swim_routes_batch"
            )
            # Latest route wins; keep when the route was first seen and how
            # many times it was revised.
            self.spark.sql(
                f"""
                MERGE INTO {self.routes} AS t
                USING (
                  SELECT *, observed_at AS first_observed_at, 1 AS revision_count,
                    current_timestamp() AS updated_at
                  FROM swim_routes_batch
                ) AS s ON t.flight_ref = s.flight_ref
                WHEN MATCHED AND s.observed_at > t.observed_at THEN UPDATE SET
                  gufi = coalesce(s.gufi, t.gufi),
                  flight_id = s.flight_id,
                  origin = coalesce(s.origin, t.origin),
                  destination = coalesce(s.destination, t.destination),
                  source_msg_type = s.source_msg_type,
                  observed_at = s.observed_at,
                  received_at = s.received_at,
                  route_string = coalesce(s.route_string, t.route_string),
                  departure_procedure =
                    coalesce(s.departure_procedure, t.departure_procedure),
                  arrival_procedure =
                    coalesce(s.arrival_procedure, t.arrival_procedure),
                  etd_type = coalesce(s.etd_type, t.etd_type),
                  etd_time = coalesce(s.etd_time, t.etd_time),
                  eta_type = coalesce(s.eta_type, t.eta_type),
                  eta_time = coalesce(s.eta_time, t.eta_time),
                  planned_altitude_ft =
                    coalesce(s.planned_altitude_ft, t.planned_altitude_ft),
                  filed_true_airspeed_kts =
                    coalesce(s.filed_true_airspeed_kts, t.filed_true_airspeed_kts),
                  waypoint_count = s.waypoint_count,
                  route_geojson = s.route_geojson,
                  waypoints_json = s.waypoints_json,
                  fixes_json = s.fixes_json,
                  sectors = s.sectors,
                  centers = s.centers,
                  airways = s.airways,
                  revision_count = t.revision_count + CASE
                    WHEN s.route_geojson <> t.route_geojson THEN 1 ELSE 0 END,
                  updated_at = s.updated_at
                WHEN MATCHED AND s.observed_at <= t.observed_at THEN UPDATE SET
                  -- Older/late message: keep the newer path, fill gaps only
                  -- (FlightSectors carry no filed route, ETD/ETA or GUFI).
                  gufi = coalesce(t.gufi, s.gufi),
                  route_string = coalesce(t.route_string, s.route_string),
                  departure_procedure =
                    coalesce(t.departure_procedure, s.departure_procedure),
                  arrival_procedure =
                    coalesce(t.arrival_procedure, s.arrival_procedure),
                  etd_type = coalesce(t.etd_type, s.etd_type),
                  etd_time = coalesce(t.etd_time, s.etd_time),
                  eta_type = coalesce(t.eta_type, s.eta_type),
                  eta_time = coalesce(t.eta_time, s.eta_time),
                  planned_altitude_ft =
                    coalesce(t.planned_altitude_ft, s.planned_altitude_ft),
                  filed_true_airspeed_kts =
                    coalesce(t.filed_true_airspeed_kts, s.filed_true_airspeed_kts),
                  first_observed_at = least(t.first_observed_at, s.observed_at)
                WHEN NOT MATCHED THEN INSERT *
                """
            )
        stored = 0
        if batch.bronze:
            stored = self.spark.sql(
                f"""
                SELECT count(DISTINCT t.event_id) AS n
                FROM {self.bronze} AS t
                JOIN swim_bronze_batch AS s ON t.event_id = s.event_id
                """
            ).collect()[0]["n"]
        if stored != len(batch.bronze):
            raise RuntimeError(
                f"SWIM Bronze verification failed: {stored} of "
                f"{len(batch.bronze)} messages present."
            )
        return {
            "bronze_written": len(batch.bronze),
            "positions_written": len(batch.positions),
            "aircraft_rows": len(batch.aircraft),
            "route_rows": len(batch.routes),
            "verified": True,
            "stats": dict(batch.stats),
        }


def _schemas() -> tuple[Any, Any]:
    try:
        from pyspark.sql.types import (
            ArrayType,
            DoubleType,
            IntegerType,
            StringType,
            StructField,
            StructType,
            TimestampType,
        )
    except ImportError:  # unit tests run without pyspark
        return None, None, None, None

    def s(name: str, kind: Any, nullable: bool = True) -> Any:
        return StructField(name, kind, nullable)

    bronze = StructType(
        [
            s("event_id", StringType(), False),
            s("received_at", TimestampType(), False),
            s("topic", StringType()),
            s("application_message_id", StringType()),
            s("payload_sha256", StringType(), False),
            s("payload_xml", StringType(), False),
            s("properties_json", StringType()),
            s("parsed_records_json", StringType()),
            s("parsed_record_count", IntegerType(), False),
            s("source", StringType(), False),
            s("parse_error", StringType()),
        ]
    )
    positions = StructType(
        [
            s("event_id", StringType(), False),
            s("bronze_event_id", StringType(), False),
            s("record_index", IntegerType(), False),
            s("flight_key", StringType(), False),
            s("flight_id", StringType(), False),
            s("gufi", StringType()),
            s("flight_ref", StringType()),
            s("msg_type", StringType()),
            s("observed_at", TimestampType(), False),
            s("received_at", TimestampType(), False),
            s("latitude", DoubleType(), False),
            s("longitude", DoubleType(), False),
            s("altitude_ft", DoubleType()),
            s("ground_speed_kts", DoubleType()),
            s("track_heading_deg", DoubleType()),
            s("vertical_rate_fpm", DoubleType()),
            s("origin", StringType()),
            s("destination", StringType()),
            s("source", StringType(), False),
            s("source_freshness_seconds", DoubleType()),
        ]
    )
    aircraft = StructType(
        [
            s("flight_ref", StringType(), False),
            s("gufi", StringType()),
            s("flight_id", StringType(), False),
            s("origin", StringType()),
            s("destination", StringType()),
            *(s(name, StringType()) for name in _AIRCRAFT_FIELDS),
            s("source_msg_type", StringType()),
            s("observed_at", TimestampType(), False),
            s("received_at", TimestampType(), False),
        ]
    )
    text_array = ArrayType(StringType())
    routes = StructType(
        [
            s("flight_ref", StringType(), False),
            s("gufi", StringType()),
            s("flight_id", StringType(), False),
            s("origin", StringType()),
            s("destination", StringType()),
            s("source_msg_type", StringType()),
            s("observed_at", TimestampType(), False),
            s("received_at", TimestampType(), False),
            s("route_string", StringType()),
            s("departure_procedure", StringType()),
            s("arrival_procedure", StringType()),
            s("etd_type", StringType()),
            s("etd_time", TimestampType()),
            s("eta_type", StringType()),
            s("eta_time", TimestampType()),
            s("planned_altitude_ft", DoubleType()),
            s("filed_true_airspeed_kts", DoubleType()),
            s("waypoint_count", IntegerType(), False),
            s("route_geojson", StringType(), False),
            s("waypoints_json", StringType(), False),
            s("fixes_json", StringType()),
            s("sectors", text_array),
            s("centers", text_array),
            s("airways", text_array),
        ]
    )
    return bronze, positions, aircraft, routes


_BRONZE_SCHEMA, _POSITION_SCHEMA, _AIRCRAFT_SCHEMA, _ROUTE_SCHEMA = _schemas()
