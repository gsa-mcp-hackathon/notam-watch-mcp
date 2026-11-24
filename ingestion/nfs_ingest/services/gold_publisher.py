"""Publish the PostGIS gold serving layer from the Delta silver tables.

Each run is a full, atomic refresh: every dataset is streamed with COPY into a
temporary staging table, then all gold tables are truncated and reloaded in a
single transaction, so MCP tools never see a half-published state. Gold is
derived data; silver in Delta remains the system of record.

Time windows are relative to the newest data in silver (not wall clock), so
publishing after the feed pauses still yields a coherent snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nfs_ingest.config import settings
from server.tool_resources.services.gold_db import connect

logger = logging.getLogger(__name__)

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "resources" / "lakebase" / (
    "gold_schema.sql"
)

# Flights not updated within this window of the newest position are dropped.
FLIGHT_STALE_HOURS = 2
# Recent track history kept per published flight.
TRACK_HOURS = 3
# Track points closer together than this are dropped (duplicate reports).
TRACK_MIN_GAP_SECONDS = 20
# NOTAMs that ended more than this long ago are not published.
NOTAM_EXPIRED_GRACE_HOURS = 24

_TS = "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX"
# Spark temp view holding NOTAM -> SUA links computed in Python at publish.
SUA_LINKS_VIEW = "nfs_gold_notam_sua_links"


@dataclass(frozen=True)
class GoldTable:
    """One gold table: staging columns (name, pg type) and the load SQL."""

    name: str
    staging_columns: tuple[tuple[str, str], ...]
    insert_sql: str
    source_sql: str
    # Extra statements run right after insert_sql (same transaction).
    post_sql: tuple[str, ...] = ()

    @property
    def staging_table(self) -> str:
        return f"stage_{self.name}"

    @property
    def column_names(self) -> list[str]:
        return [name for name, _ in self.staging_columns]


def split_statements(sql: str) -> list[str]:
    """Split gold_schema.sql on its ``-- statement`` separator lines."""
    parts = []
    for chunk in sql.split("\n-- statement\n"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        statement = "\n".join(lines).strip().rstrip(";").strip()
        if statement:
            parts.append(statement)
    return parts


def _t(name: str) -> str:
    """Backtick-quote a three-part Delta table name from settings."""
    return ".".join(f"`{part}`" for part in name.split("."))


def _json_array(column: str) -> str:
    return f"ARRAY(SELECT jsonb_array_elements_text(coalesce({column}, '[]')::jsonb))"


def published_notams_sql() -> str:
    """Current, active, non-cancel NOTAMs that gold publishes."""
    return f"""
        SELECT * FROM {_t(settings.notam_versions_table)}
        WHERE is_current AND status = 'ACTIVE' AND coalesce(notam_type, '') <> 'C'
          -- NMS staging injects synthetic test NOTAMs; keep them out of gold.
          AND coalesce(raw_text, '') NOT LIKE '%AUTOMATED TEST TEXT%'
          AND (effective_end IS NULL OR effective_end >=
               current_timestamp() - INTERVAL {NOTAM_EXPIRED_GRACE_HOURS} HOURS)
    """


_SUA_LINK_COLUMNS = (
    ("notam_version_id", "text", "STRING"),
    ("designator", "text", "STRING"),
    ("match_type", "text", "STRING"),
    ("matched_text", "text", "STRING"),
    ("lower_ft", "double precision", "DOUBLE"),
    ("lower_reference", "text", "STRING"),
    ("upper_ft", "double precision", "DOUBLE"),
    ("upper_reference", "text", "STRING"),
    ("altitude_source", "text", "STRING"),
)


def register_sua_links(spark: Any) -> int:
    """Link geometry-less published NOTAMs to the SUA they name and expose
    the links as a Spark temp view for staging. Returns the link count."""
    from nfs_ingest.services.notam_sua import SuaIndex, link_notams

    schema = ", ".join(f"{name} {kind}" for name, _, kind in _SUA_LINK_COLUMNS)
    links: list[dict[str, Any]] = []
    try:
        sua_rows = (
            spark.table(_t(settings.sua_airspaces_table))
            .select("designator_normalized", "name_code", "sua_type")
            .collect()
        )
    except Exception as exc:  # noqa: BLE001 - SUA table not loaded yet
        logger.warning("SUA reference table unavailable: %s", exc)
        sua_rows = []
    if sua_rows:
        index = SuaIndex.build(row.asDict() for row in sua_rows)
        candidates = spark.sql(
            f"""
            WITH n AS ({published_notams_sql()})
            SELECT n.notam_version_id, n.raw_text FROM n
            LEFT ANTI JOIN (SELECT DISTINCT notam_version_id
                            FROM {_t(settings.notam_hazards_table)}) h
              ON h.notam_version_id = n.notam_version_id
            WHERE n.raw_text RLIKE '^!SUA|AIRSPACE|MOA|RESTRICTED AREA|WARNING AREA'
            """
        ).collect()
        links = link_notams((row.asDict() for row in candidates), index)
    rows = [tuple(link.get(name) for name, _, _ in _SUA_LINK_COLUMNS) for link in links]
    spark.createDataFrame(rows, schema=schema).createOrReplaceTempView(SUA_LINKS_VIEW)
    return len(rows)


def gold_tables() -> list[GoldTable]:
    hazards = _t(settings.notam_hazards_table)
    nms_bronze = _t(settings.nms_bronze_table)
    sua = _t(settings.sua_airspaces_table)
    airports = _t(settings.nasr_airports_table)
    aircraft_ref = _t(settings.aircraft_reference_table)
    current = _t(settings.swim_flights_current_table)
    positions = _t(settings.swim_positions_table)
    flight_aircraft = _t(settings.swim_flight_aircraft_table)
    routes = _t(settings.swim_planned_routes_table)

    published_notams = published_notams_sql()
    newest_position = f"(SELECT max(observed_at) FROM {current})"
    published_flights = f"""
        SELECT * FROM {current}
        WHERE observed_at >= {newest_position} - INTERVAL {FLIGHT_STALE_HOURS} HOURS
          AND latitude IS NOT NULL AND longitude IS NOT NULL
    """
    # Recent radar track points (trackInformation; older rows lack msg_type).
    track_points = f"""
        WITH f AS ({published_flights}),
        p AS (
          SELECT p.flight_key, p.observed_at, p.longitude, p.latitude,
                 coalesce(p.altitude_ft, 0) AS altitude_ft,
                 lag(p.observed_at) OVER (
                   PARTITION BY p.flight_key ORDER BY p.observed_at) AS prev_at
          FROM {positions} p JOIN (SELECT flight_key AS fk FROM f) k
            ON k.fk = p.flight_key
          WHERE p.received_at >= {newest_position} - INTERVAL {TRACK_HOURS} HOURS
            AND coalesce(p.msg_type, 'trackInformation') = 'trackInformation'
        )
        SELECT * FROM p
        WHERE prev_at IS NULL
           OR unix_timestamp(observed_at) - unix_timestamp(prev_at)
              >= {TRACK_MIN_GAP_SECONDS}
    """
    latest_aircraft = f"""
        SELECT * FROM {flight_aircraft} WHERE aircraft_type IS NOT NULL
    """

    notams = GoldTable(
        name="notams",
        staging_columns=(
            ("notam_version_id", "text"),
            ("notam_id", "text"),
            ("nms_id", "text"),
            ("notam_type", "text"),
            ("status", "text"),
            ("location", "text"),
            ("category", "text"),
            ("phase_of_flight_json", "text"),
            ("effective_start", "timestamptz"),
            ("effective_end", "timestamptz"),
            ("raw_text", "text"),
            ("anchor_longitude", "double precision"),
            ("anchor_latitude", "double precision"),
            ("anchor_source", "text"),
            ("anchor_radius_nm", "double precision"),
            ("hazard_count", "integer"),
            ("schedule", "text"),
        ),
        # The item-D schedule is only in the NMS payload (bronze).
        source_sql=f"""
            WITH n AS ({published_notams}),
            h AS (SELECT notam_version_id, count(*) AS hazard_count
                  FROM {hazards} GROUP BY notam_version_id),
            s AS (
              SELECT event_id, get_json_object(
                payload_json, '$.properties.coreNOTAMData.notam.schedule')
                AS schedule
              FROM {nms_bronze}
              WHERE payload_json LIKE '%"schedule"%')
            SELECT n.notam_version_id, n.notam_id, n.nms_id, n.notam_type,
              n.status, n.location, n.category,
              to_json(coalesce(n.phase_of_flight, array())) AS phase_of_flight_json,
              date_format(n.effective_start, "{_TS}") AS effective_start,
              date_format(n.effective_end, "{_TS}") AS effective_end,
              n.raw_text, n.anchor_longitude, n.anchor_latitude, n.anchor_source,
              n.anchor_radius_nm, int(coalesce(h.hazard_count, 0)) AS hazard_count,
              nullif(trim(s.schedule), '') AS schedule
            FROM n LEFT JOIN h ON h.notam_version_id = n.notam_version_id
            LEFT JOIN s ON s.event_id = n.bronze_event_id
        """,
        insert_sql=f"""
            INSERT INTO aviation.notams
            SELECT notam_version_id, notam_id, nms_id, notam_type, status,
              location, category, {_json_array("phase_of_flight_json")},
              effective_start, effective_end, raw_text,
              CASE WHEN anchor_longitude IS NOT NULL AND anchor_latitude IS NOT NULL
                THEN ST_SetSRID(ST_MakePoint(anchor_longitude, anchor_latitude), 4326)
              END,
              anchor_source, anchor_radius_nm, hazard_count, %(published_at)s,
              schedule
            FROM stage_notams
        """,
    )

    notam_hazards = GoldTable(
        name="notam_hazards",
        staging_columns=(
            ("hazard_id", "text"),
            ("notam_version_id", "text"),
            ("notam_id", "text"),
            ("location", "text"),
            ("effective_start", "timestamptz"),
            ("effective_end", "timestamptz"),
            ("geometry_geojson", "text"),
            ("geometry_source", "text"),
            ("lower_altitude_ft", "double precision"),
            ("upper_altitude_ft", "double precision"),
            ("lower_altitude_reference", "text"),
            ("upper_altitude_reference", "text"),
            ("horizontal_radius_nm", "double precision"),
            ("parse_confidence", "double precision"),
            ("parse_warnings_json", "text"),
            ("hazard_class", "text"),
        ),
        # NMS gives aerodrome/navaid NOTAMs their feature footprint with a
        # default STD 0..UNBOUNDED (or UNKNOWN) band; those are not airspace
        # volumes. AIRSPACE text and airspace Q-codes (QR, QA, QW) always are.
        source_sql=f"""
            SELECT h.hazard_id, h.notam_version_id, h.notam_id, h.location,
              CASE
                WHEN h.geometry_source = 'OBSTACLE_POINT_BUFFER'
                  OR n.raw_text LIKE 'OBST %' THEN 'obstacle'
                WHEN n.raw_text LIKE 'AIRSPACE%'
                  OR left(n.category, 2) IN ('QR', 'QA', 'QW') THEN 'airspace'
                WHEN h.geometry_source = 'NMS_GEOJSON_AREA' AND (
                  (h.lower_altitude_reference = 'STD'
                   AND coalesce(h.lower_altitude_ft, 0) = 0
                   AND h.upper_altitude_reference = 'UNBOUNDED')
                  OR (h.lower_altitude_reference = 'UNKNOWN'
                      AND h.upper_altitude_reference = 'UNKNOWN')) THEN
                  CASE WHEN left(n.category, 2) IN ('QN', 'QI')
                         OR n.raw_text LIKE 'NAV %' THEN 'navaid'
                       ELSE 'aerodrome' END
                ELSE 'airspace'
              END AS hazard_class,
              date_format(coalesce(h.effective_start, n.effective_start), "{_TS}")
                AS effective_start,
              date_format(coalesce(h.effective_end, n.effective_end), "{_TS}")
                AS effective_end,
              h.geometry_geojson, h.geometry_source, h.lower_altitude_ft,
              h.upper_altitude_ft, h.lower_altitude_reference,
              h.upper_altitude_reference, h.horizontal_radius_nm,
              h.parse_confidence,
              to_json(coalesce(h.parse_warnings, array())) AS parse_warnings_json
            FROM {hazards} h
            JOIN ({published_notams}) n ON n.notam_version_id = h.notam_version_id
            WHERE h.geometry_geojson IS NOT NULL
        """,
        # Keep polygonal parts only; drop empty/degenerate shapes.
        insert_sql=f"""
            INSERT INTO aviation.notam_hazards
            SELECT hazard_id, notam_version_id, notam_id, location,
              effective_start, effective_end, geom, geometry_source,
              lower_altitude_ft, upper_altitude_ft, lower_altitude_reference,
              upper_altitude_reference, horizontal_radius_nm, parse_confidence,
              {_json_array("parse_warnings_json")},
              geometry_source = 'OBSTACLE_POINT_BUFFER', %(published_at)s,
              hazard_class
            FROM (
              SELECT s.*, ST_CollectionExtract(ST_MakeValid(
                ST_SetSRID(ST_GeomFromGeoJSON(geometry_geojson), 4326)), 3) AS geom
              FROM stage_notam_hazards s
            ) g
            WHERE NOT ST_IsEmpty(geom)
        """,
    )

    airports_table = GoldTable(
        name="airports",
        staging_columns=(
            ("site_no", "text"),
            ("arpt_id", "text"),
            ("icao_id", "text"),
            ("name", "text"),
            ("city", "text"),
            ("state_code", "text"),
            ("site_type_code", "text"),
            ("facility_use_code", "text"),
            ("arpt_status", "text"),
            ("resp_artcc_id", "text"),
            ("elevation_ft", "double precision"),
            ("longitude", "double precision"),
            ("latitude", "double precision"),
            ("nasr_effective_date", "date"),
        ),
        source_sql=f"""
            SELECT site_no, arpt_id, icao_id, arpt_name AS name, city, state_code,
              site_type_code, facility_use_code, arpt_status, resp_artcc_id,
              elevation_ft, longitude, latitude,
              cast(nasr_effective_date AS STRING) AS nasr_effective_date
            FROM {airports}
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        """,
        insert_sql="""
            INSERT INTO aviation.airports
            SELECT site_no, arpt_id, icao_id, name, city, state_code,
              site_type_code, facility_use_code, arpt_status, resp_artcc_id,
              elevation_ft, ST_SetSRID(ST_MakePoint(longitude, latitude), 4326),
              nasr_effective_date, %(published_at)s
            FROM stage_airports
        """,
    )

    aircraft_types = GoldTable(
        name="aircraft_types",
        staging_columns=(
            ("icao_code", "text"),
            ("manufacturer", "text"),
            ("model", "text"),
            ("engine_class", "text"),
            ("num_engines", "integer"),
            ("aircraft_approach_category", "text"),
            ("airplane_design_group", "text"),
            ("approach_speed_kt", "double precision"),
            ("wingspan_ft", "double precision"),
            ("length_ft", "double precision"),
            ("tail_height_ft", "double precision"),
            ("mtow_lb", "double precision"),
            ("icao_wtc", "text"),
            ("faa_weight_class", "text"),
            ("recat_category", "text"),
        ),
        source_sql=f"""
            SELECT icao_code, manufacturer, coalesce(model_faa, model_bada) AS model,
              engine_class, num_engines, aac AS aircraft_approach_category,
              adg AS airplane_design_group, approach_speed_kt,
              greatest(wingspan_ft_with_winglets, wingspan_ft_without_winglets)
                AS wingspan_ft,
              length_ft, tail_height_oew_ft AS tail_height_ft, mtow_lb, icao_wtc,
              faa_weight_class, recat_1_5_category AS recat_category
            FROM {aircraft_ref}
        """,
        insert_sql="""
            INSERT INTO aviation.aircraft_types
            SELECT *, %(published_at)s FROM stage_aircraft_types
        """,
    )

    flights = GoldTable(
        name="flights",
        staging_columns=(
            ("flight_key", "text"),
            ("flight_ref", "text"),
            ("gufi", "text"),
            ("callsign", "text"),
            ("origin", "text"),
            ("destination", "text"),
            ("observed_at", "timestamptz"),
            ("longitude", "double precision"),
            ("latitude", "double precision"),
            ("altitude_ft", "double precision"),
            ("ground_speed_kts", "double precision"),
            ("track_heading_deg", "double precision"),
            ("aircraft_type", "text"),
            ("aircraft_engine_class", "text"),
            ("special_aircraft_qualifier", "text"),
            ("user_category", "text"),
            ("route_flight_ref", "text"),
        ),
        # Aircraft and planned route join on flightRef, falling back to GUFI
        # (older positions predate flight_ref). Heading = bearing between the
        # last two kept track points.
        source_sql=f"""
            WITH f AS ({published_flights}),
            tp AS ({track_points}),
            last2 AS (
              SELECT flight_key, longitude, latitude,
                lead(longitude) OVER w AS prev_lon, lead(latitude) OVER w AS prev_lat,
                row_number() OVER w AS rn
              FROM tp WINDOW w AS (PARTITION BY flight_key ORDER BY observed_at DESC)
            ),
            heading AS (
              SELECT flight_key, (degrees(atan2(
                  sin(radians(longitude - prev_lon)) * cos(radians(latitude)),
                  cos(radians(prev_lat)) * sin(radians(latitude)) -
                  sin(radians(prev_lat)) * cos(radians(latitude))
                    * cos(radians(longitude - prev_lon)))) + 360) % 360
                AS track_heading_deg
              FROM last2 WHERE rn = 1 AND prev_lon IS NOT NULL
            ),
            a AS ({latest_aircraft}),
            ag AS (SELECT * FROM a WHERE gufi IS NOT NULL
                   QUALIFY row_number() OVER (
                     PARTITION BY gufi ORDER BY observed_at DESC) = 1),
            r AS (SELECT flight_ref, gufi FROM {routes}),
            rg AS (SELECT * FROM r WHERE gufi IS NOT NULL
                   QUALIFY row_number() OVER (
                     PARTITION BY gufi ORDER BY flight_ref DESC) = 1)
            SELECT f.flight_key, f.flight_ref, f.gufi, f.flight_id AS callsign,
              coalesce(f.origin, a1.origin, a2.origin) AS origin,
              coalesce(f.destination, a1.destination, a2.destination) AS destination,
              date_format(f.observed_at, "{_TS}") AS observed_at,
              f.longitude, f.latitude, f.altitude_ft, f.ground_speed_kts,
              coalesce(f.track_heading_deg, h.track_heading_deg) AS track_heading_deg,
              coalesce(a1.aircraft_type, a2.aircraft_type) AS aircraft_type,
              coalesce(a1.aircraft_engine_class, a2.aircraft_engine_class)
                AS aircraft_engine_class,
              coalesce(a1.special_aircraft_qualifier, a2.special_aircraft_qualifier)
                AS special_aircraft_qualifier,
              coalesce(a1.user_category, a2.user_category) AS user_category,
              coalesce(r1.flight_ref, r2.flight_ref) AS route_flight_ref
            FROM f
            LEFT JOIN heading h ON h.flight_key = f.flight_key
            LEFT JOIN a a1 ON a1.flight_ref = f.flight_ref
            LEFT JOIN ag a2 ON a2.gufi = f.gufi
            LEFT JOIN r r1 ON r1.flight_ref = f.flight_ref
            LEFT JOIN rg r2 ON r2.gufi = f.gufi
        """,
        insert_sql="""
            INSERT INTO aviation.flights
            SELECT flight_key, flight_ref, gufi, callsign, origin, destination,
              observed_at, ST_SetSRID(ST_MakePoint(longitude, latitude), 4326),
              altitude_ft, ground_speed_kts, track_heading_deg, aircraft_type,
              aircraft_engine_class, special_aircraft_qualifier, user_category,
              route_flight_ref, %(published_at)s
            FROM stage_flights
        """,
    )

    flight_routes = GoldTable(
        name="flight_routes",
        staging_columns=(
            ("flight_ref", "text"),
            ("gufi", "text"),
            ("callsign", "text"),
            ("origin", "text"),
            ("destination", "text"),
            ("route_string", "text"),
            ("departure_procedure", "text"),
            ("arrival_procedure", "text"),
            ("etd_type", "text"),
            ("etd_time", "timestamptz"),
            ("eta_type", "text"),
            ("eta_time", "timestamptz"),
            ("planned_altitude_ft", "double precision"),
            ("filed_true_airspeed_kts", "double precision"),
            ("route_geojson", "text"),
            ("waypoints_json", "text"),
            ("fixes_json", "text"),
            ("sectors_json", "text"),
            ("centers_json", "text"),
            ("airways_json", "text"),
            ("observed_at", "timestamptz"),
            ("revision_count", "integer"),
        ),
        source_sql=f"""
            SELECT flight_ref, gufi, flight_id AS callsign, origin, destination,
              route_string, departure_procedure, arrival_procedure, etd_type,
              date_format(etd_time, "{_TS}") AS etd_time, eta_type,
              date_format(eta_time, "{_TS}") AS eta_time, planned_altitude_ft,
              filed_true_airspeed_kts, route_geojson, waypoints_json, fixes_json,
              to_json(coalesce(sectors, array())) AS sectors_json,
              to_json(coalesce(centers, array())) AS centers_json,
              to_json(coalesce(airways, array())) AS airways_json,
              date_format(observed_at, "{_TS}") AS observed_at, revision_count
            FROM {routes}
            WHERE waypoint_count >= 2
        """,
        insert_sql=f"""
            INSERT INTO aviation.flight_routes
            SELECT flight_ref, gufi, callsign, origin, destination, route_string,
              departure_procedure, arrival_procedure, etd_type, etd_time, eta_type,
              eta_time, planned_altitude_ft, filed_true_airspeed_kts,
              ST_SetSRID(ST_GeomFromGeoJSON(route_geojson), 4326),
              waypoints_json::jsonb, fixes_json::jsonb,
              {_json_array("sectors_json")}, {_json_array("centers_json")},
              {_json_array("airways_json")}, observed_at, revision_count,
              %(published_at)s
            FROM stage_flight_routes
        """,
    )

    flight_tracks = GoldTable(
        name="flight_tracks",
        staging_columns=(
            ("flight_key", "text"),
            ("track_wkt", "text"),
            ("first_observed_at", "timestamptz"),
            ("last_observed_at", "timestamptz"),
            ("point_count", "integer"),
        ),
        source_sql=f"""
            WITH tp AS ({track_points}),
            g AS (
              SELECT flight_key,
                array_sort(collect_list(struct(observed_at, longitude, latitude,
                  altitude_ft))) AS pts
              FROM tp GROUP BY flight_key HAVING count(*) >= 2
            )
            SELECT flight_key,
              concat('LINESTRING Z (', concat_ws(', ', transform(pts, p ->
                concat_ws(' ', cast(p.longitude AS STRING),
                  cast(p.latitude AS STRING), cast(p.altitude_ft AS STRING)))),
                ')') AS track_wkt,
              date_format(pts[0].observed_at, "{_TS}") AS first_observed_at,
              date_format(element_at(pts, -1).observed_at, "{_TS}")
                AS last_observed_at,
              size(pts) AS point_count
            FROM g
        """,
        insert_sql="""
            INSERT INTO aviation.flight_tracks
            SELECT flight_key, ST_GeomFromText(track_wkt, 4326), first_observed_at,
              last_observed_at, point_count, %(published_at)s
            FROM stage_flight_tracks
        """,
    )

    # BASE + UNION components minus SUBTR components, combined in PostGIS.
    def _components(operations: str) -> str:
        return f"""
            SELECT ST_Union(ST_MakeValid(ST_SetSRID(
              ST_GeomFromGeoJSON((c->'geometry')::text), 4326)))
            FROM jsonb_array_elements(s.components_json::jsonb) c
            WHERE c->>'operation' IN ({operations})
        """

    sua_airspaces = GoldTable(
        name="sua_airspaces",
        staging_columns=(
            ("designator", "text"),
            ("name", "text"),
            ("name_code", "text"),
            ("sua_type", "text"),
            ("administrative_area", "text"),
            ("lower_ft", "double precision"),
            ("lower_reference", "text"),
            ("upper_ft", "double precision"),
            ("upper_reference", "text"),
            ("components_json", "text"),
            ("schedule_json", "text"),
            ("nasr_effective_date", "date"),
        ),
        source_sql=f"""
            SELECT designator_normalized AS designator, name, name_code, sua_type,
              administrative_area, lower_ft, lower_reference, upper_ft,
              upper_reference, components_json, schedule_json,
              cast(nasr_effective_date AS STRING) AS nasr_effective_date
            FROM {sua}
        """,
        insert_sql=f"""
            INSERT INTO aviation.sua_airspaces
            SELECT designator, name, name_code, sua_type, administrative_area,
              lower_ft, lower_reference, upper_ft, upper_reference, g.geom,
              schedule_json::jsonb, nasr_effective_date, %(published_at)s
            FROM stage_sua_airspaces s
            CROSS JOIN LATERAL (
              SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(
                CASE WHEN sub.g IS NULL THEN add.g
                     ELSE ST_Difference(add.g, sub.g) END), 3)) AS geom
              FROM ({_components("'BASE', 'UNION'")}) add(g),
                   ({_components("'SUBTR'")}) sub(g)
            ) g
            WHERE g.geom IS NOT NULL AND NOT ST_IsEmpty(g.geom)
        """,
    )

    # Links become SUA_REFERENCE hazard shapes. Altitudes: the band the
    # NOTAM activates when stated, else the airspace's published limits.
    notam_sua_links = GoldTable(
        name="notam_sua_links",
        staging_columns=tuple((name, pg) for name, pg, _ in _SUA_LINK_COLUMNS),
        source_sql=f"SELECT * FROM {SUA_LINKS_VIEW}",
        insert_sql="""
            INSERT INTO aviation.notam_sua_links
            SELECT DISTINCT ON (notam_version_id, designator) *, %(published_at)s
            FROM stage_notam_sua_links
            WHERE designator IN (SELECT designator FROM aviation.sua_airspaces)
              AND notam_version_id IN (SELECT notam_version_id FROM aviation.notams)
        """,
        post_sql=(
            """
            INSERT INTO aviation.notam_hazards (
              hazard_id, notam_version_id, notam_id, location, effective_start,
              effective_end, geom, geometry_source, lower_altitude_ft,
              upper_altitude_ft, lower_altitude_reference,
              upper_altitude_reference, horizontal_radius_nm, parse_confidence,
              parse_warnings, is_obstacle, published_at, hazard_class)
            SELECT 'sua:' || l.notam_version_id || ':' || l.designator,
              l.notam_version_id, n.notam_id, n.location, n.effective_start,
              n.effective_end, a.geom, 'SUA_REFERENCE',
              CASE WHEN l.altitude_source = 'notam_text' THEN l.lower_ft
                   ELSE a.lower_ft END,
              CASE WHEN l.altitude_source = 'notam_text' THEN l.upper_ft
                   ELSE a.upper_ft END,
              CASE WHEN l.altitude_source = 'notam_text' THEN l.lower_reference
                   ELSE a.lower_reference END,
              CASE WHEN l.altitude_source = 'notam_text' THEN l.upper_reference
                   ELSE a.upper_reference END,
              NULL,
              CASE l.match_type WHEN 'designator_group' THEN 0.8 ELSE 0.9 END,
              ARRAY[
                'shape from FAA SAA ' || a.name || ' (NASR '
                  || a.nasr_effective_date || ')',
                CASE WHEN l.altitude_source = 'notam_text'
                  THEN 'altitudes from NOTAM activation text'
                  ELSE 'altitudes are the airspace published limits' END],
              false, %(published_at)s, 'airspace'
            FROM aviation.notam_sua_links l
            JOIN aviation.notams n ON n.notam_version_id = l.notam_version_id
            JOIN aviation.sua_airspaces a ON a.designator = l.designator
            ON CONFLICT (hazard_id) DO NOTHING
            """,
            """
            UPDATE aviation.notams n SET hazard_count = n.hazard_count + l.k
            FROM (SELECT notam_version_id, count(*) AS k
                  FROM aviation.notam_sua_links GROUP BY 1) l
            WHERE l.notam_version_id = n.notam_version_id
            """,
        ),
    )

    return [
        notams,
        notam_hazards,
        airports_table,
        aircraft_types,
        flights,
        flight_routes,
        flight_tracks,
        sua_airspaces,
        notam_sua_links,
    ]


def _stream_rows(dataframe: Any, columns: list[str]):
    for row in dataframe.select(*columns).toLocalIterator():
        yield tuple(row[c] for c in columns)


def publish_gold(spark: Any, *, apply_schema: bool = True) -> dict[str, Any]:
    """Refresh every gold table from silver in one Postgres transaction."""
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    published_at = datetime.now(UTC)
    sua_links = register_sua_links(spark)
    tables = gold_tables()
    staged: dict[str, int] = {}
    loaded: dict[str, int] = {}

    with connect(autocommit=False, statement_timeout_ms=None) as connection:
        with connection.cursor() as cur:
            if apply_schema:
                for statement in split_statements(SCHEMA_SQL.read_text()):
                    cur.execute(statement)
                connection.commit()

            for table in tables:
                columns = ", ".join(f"{n} {t}" for n, t in table.staging_columns)
                cur.execute(
                    f"CREATE TEMP TABLE {table.staging_table} ({columns}) "
                    "ON COMMIT DROP"
                )
                dataframe = spark.sql(table.source_sql)
                count = 0
                copy_sql = (
                    f"COPY {table.staging_table} ({', '.join(table.column_names)}) "
                    "FROM STDIN"
                )
                with cur.copy(copy_sql) as copy:
                    for values in _stream_rows(dataframe, table.column_names):
                        copy.write_row(values)
                        count += 1
                staged[table.name] = count
                logger.info("staged %s rows for %s", count, table.name)

            # Swap: all tables change together when the transaction commits.
            for table in tables:
                cur.execute(f"TRUNCATE aviation.{table.name}")
                cur.execute(table.insert_sql, {"published_at": published_at})
                loaded[table.name] = cur.rowcount
                for statement in table.post_sql:
                    params = (
                        {"published_at": published_at}
                        if "%(published_at)s" in statement
                        else None
                    )
                    cur.execute(statement, params)
                    if table.name == "notam_sua_links" and "INSERT" in statement:
                        loaded["sua_reference_hazards"] = cur.rowcount
                cur.execute(
                    "INSERT INTO aviation.publish_log "
                    "(published_at, table_name, row_count) VALUES (%s, %s, %s)",
                    (published_at, table.name, loaded[table.name]),
                )
            connection.commit()
            cur.execute("ANALYZE aviation.notam_hazards")
            cur.execute("ANALYZE aviation.flights")
            cur.execute("ANALYZE aviation.flight_routes")
            connection.commit()

    dropped = {
        name: staged[name] - loaded.get(name, 0)
        for name in staged
        if staged[name] != loaded.get(name, 0)
    }
    loaded["sua_links_found"] = sua_links
    return {
        "operation": "gold-publish",
        "published_at": published_at.isoformat(),
        "rows": loaded,
        "rows_dropped_in_load": dropped,
    }
