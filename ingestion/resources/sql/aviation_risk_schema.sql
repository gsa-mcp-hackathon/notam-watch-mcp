-- NFS aviation risk model for the current development schema.
-- Change the three-part identifiers before deploying to another environment.

CREATE TABLE IF NOT EXISTS
  `users`.`nfs_demo`.`nfs_ingestion_watermarks` (
  feed_name STRING NOT NULL,
  last_successful_pull_at TIMESTAMP,
  last_run_started_at TIMESTAMP,
  last_run_completed_at TIMESTAMP,
  last_run_mode STRING,
  last_run_status STRING,
  last_request_id STRING,
  records_written BIGINT,
  error_detail STRING,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_nms_notam_events_raw` (
  event_id STRING NOT NULL,
  nms_id STRING,
  fetched_at TIMESTAMP NOT NULL,
  last_updated_at TIMESTAMP,
  request_mode STRING NOT NULL,
  request_watermark TIMESTAMP,
  payload_sha256 STRING NOT NULL,
  payload_json STRING NOT NULL,
  source STRING NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_notam_versions` (
  notam_version_id STRING NOT NULL,
  bronze_event_id STRING NOT NULL,
  nms_id STRING NOT NULL,
  notam_id STRING,
  notam_type STRING,
  status STRING NOT NULL,
  location STRING,
  replaces_notam_version_id STRING,
  effective_start TIMESTAMP NOT NULL,
  effective_end TIMESTAMP,
  raw_text STRING,
  category STRING,
  phase_of_flight ARRAY<STRING>,
  valid_from TIMESTAMP NOT NULL,
  valid_to TIMESTAMP,
  is_current BOOLEAN NOT NULL,
  parsed_at TIMESTAMP NOT NULL,
  parser_version STRING NOT NULL,
  source STRING NOT NULL,
  -- Location anchor for every NOTAM (not a hazard volume). See
  -- migrations/002_notam_version_anchor.sql.
  anchor_longitude DOUBLE,
  anchor_latitude DOUBLE,
  anchor_h3_r7 BIGINT,
  anchor_source STRING,
  anchor_radius_nm DOUBLE,
  anchor_qline_offset_nm DOUBLE
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- FAA NASR APT_BASE landing facilities, replaced each 28-day cycle by the
-- nasr_airports_ingest job (ingestion/nfs_ingest/services/nasr_airports.py).
CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_airports` (
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
COMMENT 'FAA NASR APT_BASE landing facilities (28-day cycle).';

-- Hazard geometry_source values include NMS_GEOJSON_AREA, TEXT_CIRCLE,
-- TEXT_POLYGON and OBSTACLE_POINT_BUFFER (OBST point NOTAMs with a published
-- MSL top: SFC-to-MSL cylinder, horizontal_buffer_nm = modeling buffer).
CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_notam_hazard_volumes` (
  hazard_id STRING NOT NULL,
  notam_version_id STRING NOT NULL,
  nms_id STRING NOT NULL,
  notam_id STRING,
  location STRING,
  effective_start TIMESTAMP NOT NULL,
  effective_end TIMESTAMP,
  geometry_geojson STRING NOT NULL,
  geometry_source STRING NOT NULL,
  lower_altitude_ft DOUBLE,
  upper_altitude_ft DOUBLE,
  lower_altitude_reference STRING NOT NULL,
  upper_altitude_reference STRING NOT NULL,
  horizontal_radius_nm DOUBLE NOT NULL,
  circle_center_longitude DOUBLE,
  circle_center_latitude DOUBLE,
  horizontal_buffer_nm DOUBLE NOT NULL,
  h3_resolution INT NOT NULL,
  h3_cells ARRAY<BIGINT> NOT NULL,
  parse_confidence DOUBLE NOT NULL,
  parse_warnings ARRAY<STRING> NOT NULL,
  parser_version STRING NOT NULL,
  source STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_flight_position_events` (
  event_id STRING NOT NULL,
  bronze_event_id STRING NOT NULL,
  record_index INT NOT NULL,
  flight_key STRING NOT NULL,
  flight_id STRING NOT NULL,
  gufi STRING,
  observed_at TIMESTAMP NOT NULL,
  received_at TIMESTAMP NOT NULL,
  latitude DOUBLE NOT NULL,
  longitude DOUBLE NOT NULL,
  altitude_ft DOUBLE,
  ground_speed_kts DOUBLE,
  track_heading_deg DOUBLE,
  vertical_rate_fpm DOUBLE,
  origin STRING,
  destination STRING,
  h3_r7 BIGINT,
  source STRING NOT NULL,
  source_freshness_seconds DOUBLE
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_flights_current` (
  flight_key STRING NOT NULL,
  event_id STRING NOT NULL,
  flight_id STRING NOT NULL,
  gufi STRING,
  observed_at TIMESTAMP NOT NULL,
  received_at TIMESTAMP NOT NULL,
  latitude DOUBLE NOT NULL,
  longitude DOUBLE NOT NULL,
  altitude_ft DOUBLE,
  ground_speed_kts DOUBLE,
  track_heading_deg DOUBLE,
  vertical_rate_fpm DOUBLE,
  origin STRING,
  destination STRING,
  h3_r7 BIGINT,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_flight_trajectory_segments` (
  segment_id STRING NOT NULL,
  flight_key STRING NOT NULL,
  start_event_id STRING NOT NULL,
  end_event_id STRING,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP NOT NULL,
  path_geojson STRING NOT NULL,
  start_altitude_ft DOUBLE,
  end_altitude_ft DOUBLE,
  min_altitude_ft DOUBLE,
  max_altitude_ft DOUBLE,
  average_ground_speed_kts DOUBLE,
  initial_heading_deg DOUBLE,
  final_heading_deg DOUBLE,
  h3_cells_r7 ARRAY<BIGINT> NOT NULL,
  is_projected BOOLEAN NOT NULL,
  projection_horizon_seconds INT NOT NULL,
  algorithm_version STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_notam_flight_encounters` (
  encounter_id STRING NOT NULL,
  flight_key STRING NOT NULL,
  hazard_id STRING NOT NULL,
  first_detected_at TIMESTAMP NOT NULL,
  predicted_entry_at TIMESTAMP,
  actual_entry_at TIMESTAMP,
  closest_approach_at TIMESTAMP,
  cleared_at TIMESTAMP,
  state STRING NOT NULL,
  horizontal_intersection BOOLEAN NOT NULL,
  vertical_overlap BOOLEAN NOT NULL,
  temporal_overlap BOOLEAN NOT NULL,
  minimum_horizontal_distance_nm DOUBLE,
  minimum_vertical_separation_ft DOUBLE,
  time_to_entry_seconds INT,
  rule_version STRING NOT NULL,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_flight_maneuvers` (
  maneuver_id STRING NOT NULL,
  flight_key STRING NOT NULL,
  encounter_id STRING,
  detected_at TIMESTAMP NOT NULL,
  maneuver_type STRING NOT NULL,
  heading_change_deg DOUBLE,
  altitude_change_ft DOUBLE,
  speed_change_kts DOUBLE,
  cross_track_deviation_nm DOUBLE,
  distance_to_hazard_nm DOUBLE,
  avoidance_likelihood DOUBLE,
  detector_version STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_notam_response_baselines` (
  baseline_id STRING NOT NULL,
  hazard_cluster_key STRING NOT NULL,
  route_cluster_key STRING,
  aircraft_class STRING,
  sample_count BIGINT NOT NULL,
  median_lateral_deviation_nm DOUBLE NOT NULL,
  p90_lateral_deviation_nm DOUBLE NOT NULL,
  median_altitude_change_ft DOUBLE NOT NULL,
  median_warning_lead_seconds DOUBLE NOT NULL,
  maneuver_distribution MAP<STRING, DOUBLE> NOT NULL,
  window_start TIMESTAMP NOT NULL,
  window_end TIMESTAMP NOT NULL,
  model_version STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_flight_notam_risk_scores` (
  risk_id STRING NOT NULL,
  encounter_id STRING NOT NULL,
  scored_at TIMESTAMP NOT NULL,
  valid_until TIMESTAMP NOT NULL,
  score DOUBLE NOT NULL,
  severity STRING NOT NULL,
  trajectory_conflict_score DOUBLE NOT NULL,
  route_deviation_score DOUBLE NOT NULL,
  peer_response_score DOUBLE NOT NULL,
  data_quality_score DOUBLE NOT NULL,
  reasons ARRAY<STRING> NOT NULL,
  rule_version STRING NOT NULL,
  model_version STRING
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

CREATE TABLE IF NOT EXISTS `users`.`nfs_demo`.`nfs_warning_events` (
  warning_id STRING NOT NULL,
  risk_id STRING NOT NULL,
  flight_key STRING NOT NULL,
  hazard_id STRING NOT NULL,
  issued_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  severity STRING NOT NULL,
  status STRING NOT NULL,
  message STRING NOT NULL,
  acknowledged_at TIMESTAMP,
  resolved_at TIMESTAMP,
  updated_at TIMESTAMP NOT NULL
) USING DELTA
TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');
