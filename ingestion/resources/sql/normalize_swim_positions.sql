-- Expand parsed R14 records from the validated Bronze table.
-- This view is the idempotent source for a MERGE into nfs_flight_position_events.

CREATE OR REPLACE VIEW
  `users`.`nfs_demo`.`nfs_flight_position_events_source`
AS
SELECT
  sha2(concat_ws(':', raw.event_id, cast(record_index AS STRING)), 256) AS event_id,
  raw.event_id AS bronze_event_id,
  record_index,
  coalesce(nullif(record.gufi, ''), concat_ws(':', record.flight_id,
    record.origin, record.destination)) AS flight_key,
  record.flight_id,
  nullif(record.gufi, '') AS gufi,
  coalesce(try_cast(record.event_time AS TIMESTAMP), raw.received_at) AS observed_at,
  raw.received_at,
  record.current_lat AS latitude,
  record.current_lon AS longitude,
  record.current_altitude_ft AS altitude_ft,
  record.velocity_kts AS ground_speed_kts,
  cast(NULL AS DOUBLE) AS track_heading_deg,
  cast(NULL AS DOUBLE) AS vertical_rate_fpm,
  nullif(record.origin, '') AS origin,
  nullif(record.destination, '') AS destination,
  h3_longlatash3(record.current_lon, record.current_lat, 7) AS h3_r7,
  record.source,
  greatest(
    0D,
    timestampdiff(MILLISECOND,
      coalesce(try_cast(record.event_time AS TIMESTAMP), raw.received_at),
      raw.received_at
    ) / 1000D
  ) AS source_freshness_seconds
FROM `users`.`nfs_demo`.`nfs_swim_flight_events_raw` AS raw
LATERAL VIEW posexplode(from_json(
  raw.parsed_records_json,
  'ARRAY<STRUCT<flight_id:STRING,gufi:STRING,origin:STRING,destination:STRING,current_lat:DOUBLE,current_lon:DOUBLE,current_altitude_ft:DOUBLE,velocity_kts:DOUBLE,event_time:STRING,source:STRING>>'
)) exploded AS record_index, record
WHERE record.flight_id IS NOT NULL
  AND record.current_lat BETWEEN -90 AND 90
  AND record.current_lon BETWEEN -180 AND 180
  AND NOT (record.current_lat = 0 AND record.current_lon = 0);

MERGE INTO `users`.`nfs_demo`.`nfs_flight_position_events` AS target
USING `users`.`nfs_demo`.`nfs_flight_position_events_source` AS source
ON target.event_id = source.event_id
WHEN NOT MATCHED THEN INSERT *;

CREATE OR REPLACE VIEW
  `users`.`nfs_demo`.`nfs_active_notam_flight_candidates`
AS
SELECT
  flight.flight_key,
  flight.event_id AS flight_event_id,
  hazard.hazard_id,
  flight.observed_at,
  flight.altitude_ft,
  flight.h3_r7,
  hazard.effective_start,
  hazard.effective_end,
  hazard.lower_altitude_ft,
  hazard.upper_altitude_ft
FROM `users`.`nfs_demo`.`nfs_flight_position_events` AS flight
JOIN `users`.`nfs_demo`.`nfs_notam_hazard_volumes` AS hazard
  ON array_contains(hazard.h3_cells, flight.h3_r7)
  AND flight.observed_at >= hazard.effective_start
  AND (hazard.effective_end IS NULL OR flight.observed_at <= hazard.effective_end)
  AND (
    flight.altitude_ft IS NULL
    OR (
      (hazard.lower_altitude_ft IS NULL
        OR flight.altitude_ft >= hazard.lower_altitude_ft)
      AND (hazard.upper_altitude_ft IS NULL
        OR flight.altitude_ft <= hazard.upper_altitude_ft)
    )
  )
JOIN `users`.`nfs_demo`.`nfs_notam_versions` AS notam
  ON notam.notam_version_id = hazard.notam_version_id
  AND notam.is_current = true
  AND notam.status = 'ACTIVE';
