-- PostGIS gold serving layer for the NFS MCP server (Lakebase project
-- nfs-aviation-postgis, database databricks_postgres, schema aviation).
--
-- Written by nfs_ingest.services.gold_publisher (full refresh per run) from the
-- Delta silver tables in users.nfs_demo. Read by the MCP tools in
-- server.tools.flight_notam. All geometry is WGS84 (SRID 4326); distances in
-- the tools use ::geography (metres).
--
-- Idempotent. Statements are separated by a line containing only
-- "-- statement" so the publisher can execute them one at a time.

CREATE EXTENSION IF NOT EXISTS postgis;
-- statement
CREATE SCHEMA IF NOT EXISTS aviation;
-- statement
CREATE TABLE IF NOT EXISTS aviation.notams (
  notam_version_id text PRIMARY KEY,
  notam_id text,
  nms_id text,
  notam_type text,
  status text,
  location text,
  category text,
  phase_of_flight text[],
  effective_start timestamptz,
  effective_end timestamptz,          -- NULL = open-ended / permanent
  raw_text text,
  anchor geometry(Point, 4326),       -- best single location (anchor_source)
  anchor_source text,
  anchor_radius_nm double precision,
  hazard_count integer NOT NULL DEFAULT 0,
  published_at timestamptz NOT NULL,
  schedule text                        -- ICAO item D, e.g. 'DLY 1230-2330'
);
-- statement
ALTER TABLE aviation.notams ADD COLUMN IF NOT EXISTS schedule text;
-- statement
CREATE TABLE IF NOT EXISTS aviation.notam_hazards (
  hazard_id text PRIMARY KEY,
  notam_version_id text NOT NULL,
  notam_id text,
  location text,
  effective_start timestamptz,
  effective_end timestamptz,
  geom geometry(Geometry, 4326) NOT NULL,   -- (multi)polygon footprint
  geometry_source text,                      -- NMS_GEOJSON_AREA, TEXT_CIRCLE..
  lower_altitude_ft double precision,
  upper_altitude_ft double precision,
  lower_altitude_reference text,             -- SFC, MSL, AGL, STD, UNKNOWN
  upper_altitude_reference text,             -- MSL, AGL, STD, UNBOUNDED, ..
  horizontal_radius_nm double precision,
  parse_confidence double precision,
  parse_warnings text[],
  is_obstacle boolean NOT NULL,
  published_at timestamptz NOT NULL,
  -- airspace: restricted/UAS/TFR-style volumes with a real altitude band;
  -- obstacle: OBST point cylinders; aerodrome / navaid: NMS feature
  -- footprints (runway, taxiway, lighting, ILS, NAVAID) whose STD..UNBOUNDED
  -- band is a default, not a published altitude.
  hazard_class text NOT NULL DEFAULT 'airspace'
);
-- statement
ALTER TABLE aviation.notam_hazards
  ADD COLUMN IF NOT EXISTS hazard_class text NOT NULL DEFAULT 'airspace';
-- statement
CREATE INDEX IF NOT EXISTS notam_hazards_class_idx
  ON aviation.notam_hazards (hazard_class);
-- statement
CREATE TABLE IF NOT EXISTS aviation.airports (
  site_no text NOT NULL,              -- NASR site number (+ arpt_id = key)
  arpt_id text NOT NULL,              -- FAA location id, e.g. JFK
  icao_id text,                       -- e.g. KJFK
  name text,
  city text,
  state_code text,
  site_type_code text,                -- A airport, H heliport, ...
  facility_use_code text,
  arpt_status text,
  resp_artcc_id text,
  elevation_ft double precision,
  geom geometry(Point, 4326) NOT NULL,
  nasr_effective_date date,
  published_at timestamptz NOT NULL,
  PRIMARY KEY (site_no, arpt_id)
);
-- statement
CREATE TABLE IF NOT EXISTS aviation.aircraft_types (
  icao_code text PRIMARY KEY,
  manufacturer text,
  model text,
  engine_class text,
  num_engines integer,
  aircraft_approach_category text,    -- AAC (A-E)
  airplane_design_group text,         -- ADG (I-VI)
  approach_speed_kt double precision,
  wingspan_ft double precision,       -- max of with/without winglets
  length_ft double precision,
  tail_height_ft double precision,
  mtow_lb double precision,           -- maximum certificated, not actual
  icao_wtc text,
  faa_weight_class text,
  recat_category text,
  published_at timestamptz NOT NULL
);
-- statement
CREATE TABLE IF NOT EXISTS aviation.flights (
  flight_key text PRIMARY KEY,
  flight_ref text,
  gufi text,
  callsign text NOT NULL,
  origin text,
  destination text,
  observed_at timestamptz NOT NULL,
  geom geometry(Point, 4326) NOT NULL,
  altitude_ft double precision,
  ground_speed_kts double precision,
  track_heading_deg double precision, -- derived from the last track points
  aircraft_type text,
  aircraft_engine_class text,
  special_aircraft_qualifier text,
  user_category text,
  route_flight_ref text,              -- flight_routes key when a plan exists
  published_at timestamptz NOT NULL
);
-- statement
CREATE TABLE IF NOT EXISTS aviation.flight_routes (
  flight_ref text PRIMARY KEY,
  gufi text,
  callsign text,
  origin text,
  destination text,
  route_string text,
  departure_procedure text,
  arrival_procedure text,
  etd_type text,
  etd_time timestamptz,
  eta_type text,
  eta_time timestamptz,
  planned_altitude_ft double precision,
  filed_true_airspeed_kts double precision,
  geom geometry(LineString, 4326) NOT NULL, -- TFMS modeled planned path
  waypoints jsonb,                    -- [{seq, lat, lon, elapsed_s}]
  fixes jsonb,                        -- [{seq, name, elapsed_s}]
  sectors text[],
  centers text[],
  airways text[],
  observed_at timestamptz,
  revision_count integer,
  published_at timestamptz NOT NULL
);
-- statement
CREATE TABLE IF NOT EXISTS aviation.flight_tracks (
  flight_key text PRIMARY KEY,
  geom geometry(LineStringZ, 4326) NOT NULL, -- Z = altitude in feet
  first_observed_at timestamptz NOT NULL,
  last_observed_at timestamptz NOT NULL,
  point_count integer NOT NULL,
  published_at timestamptz NOT NULL
);
-- statement
CREATE TABLE IF NOT EXISTS aviation.publish_log (
  published_at timestamptz NOT NULL,
  table_name text NOT NULL,
  row_count integer NOT NULL,
  source_max_timestamp timestamptz,
  PRIMARY KEY (published_at, table_name)
);
-- statement
CREATE INDEX IF NOT EXISTS notams_location_idx ON aviation.notams (location);
-- statement
CREATE INDEX IF NOT EXISTS notams_notam_id_idx ON aviation.notams (notam_id);
-- statement
CREATE INDEX IF NOT EXISTS notams_anchor_gix ON aviation.notams USING gist (anchor);
-- statement
CREATE INDEX IF NOT EXISTS notam_hazards_geom_gix
  ON aviation.notam_hazards USING gist (geom);
-- statement
CREATE INDEX IF NOT EXISTS notam_hazards_geog_gix
  ON aviation.notam_hazards USING gist ((geom::geography));
-- statement
CREATE INDEX IF NOT EXISTS notam_hazards_version_idx
  ON aviation.notam_hazards (notam_version_id);
-- statement
CREATE INDEX IF NOT EXISTS airports_arpt_idx ON aviation.airports (arpt_id);
-- statement
CREATE INDEX IF NOT EXISTS airports_icao_idx ON aviation.airports (icao_id);
-- statement
CREATE INDEX IF NOT EXISTS flights_callsign_idx ON aviation.flights (callsign);
-- statement
CREATE INDEX IF NOT EXISTS flights_geog_gix
  ON aviation.flights USING gist ((geom::geography));
-- statement
CREATE INDEX IF NOT EXISTS flight_routes_od_idx
  ON aviation.flight_routes (origin, destination);
-- statement
CREATE INDEX IF NOT EXISTS flight_routes_geom_gix
  ON aviation.flight_routes USING gist (geom);
-- statement
CREATE INDEX IF NOT EXISTS flight_tracks_geom_gix
  ON aviation.flight_tracks USING gist (geom);
-- statement
-- FAA Special Use Airspace (SAA AIXM, NASR cycle): MOA, restricted (R),
-- warning (W), alert (A), prohibited (P) areas and NSAs. Published limits
-- and normal schedule (AIXM timesheets) are the charted defaults; a NOTAM
-- activation can narrow them.
CREATE TABLE IF NOT EXISTS aviation.sua_airspaces (
  designator text PRIMARY KEY,               -- normalized: R5107K, MLIVEOAK
  name text,                                 -- 'R-5107K WHITE SANDS MISSILE ..'
  name_code text,                            -- 'LIVE OAK MOA'
  sua_type text,                             -- MOA, RA, WA, AA, PA, NSA
  administrative_area text,
  lower_ft double precision,
  lower_reference text,
  upper_ft double precision,
  upper_reference text,
  geom geometry(MultiPolygon, 4326) NOT NULL,
  schedule jsonb,
  nasr_effective_date date,
  published_at timestamptz NOT NULL
);
-- statement
CREATE INDEX IF NOT EXISTS sua_airspaces_geog_gix
  ON aviation.sua_airspaces USING gist ((geom::geography));
-- statement
-- NOTAMs that name an SUA (activation or TFR boundary). Each link also
-- becomes a SUA_REFERENCE row in notam_hazards.
CREATE TABLE IF NOT EXISTS aviation.notam_sua_links (
  notam_version_id text NOT NULL,
  designator text NOT NULL,
  match_type text,                           -- designator, designator_group, name
  matched_text text,
  lower_ft double precision,                 -- activated band from NOTAM text
  lower_reference text,
  upper_ft double precision,
  upper_reference text,
  altitude_source text,                      -- notam_text | sua_published
  published_at timestamptz NOT NULL,
  PRIMARY KEY (notam_version_id, designator)
);
