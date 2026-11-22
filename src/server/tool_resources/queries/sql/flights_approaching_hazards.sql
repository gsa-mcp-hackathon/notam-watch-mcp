-- Live flights inside, or projected to enter within the horizon, active NOTAM
-- hazard areas. The core spatial query of get_flights_approaching_notams.
--
-- hz     active hazard areas matching the filters (NOTAM id, location, one
--        special use airspace, a circle around a point, hazard_type).
-- fl     fresh flight positions, each with how far it flies in the horizon
--        (ahead_m, at least 120 kt) and its planned route.
-- cand   flight x hazard pairs close enough to matter (bounding-box
--        prefilter, then an exact distance test).
-- paths  each flight's projected path: along the remaining planned route if
--        within 20 NM of it, else a straight line on its current track.
-- hits   pairs whose path or position touches the hazard, with the entry
--        point along the path.
--
-- Params: notam_id, locations, sua_like, center (EWKT point), radius_m,
--   include_obstacles, now, horizon (minutes), fresh (minutes), max_hazards.
-- Slots: hazard_type (predicate from common.hazard_type_predicate).
WITH hz AS (
  SELECT h.geom AS hgeom, md5(ST_AsBinary(h.geom)) AS geom_key,
    {hazard_fields}
  FROM aviation.notam_hazards h
  JOIN aviation.notams n ON n.notam_version_id = h.notam_version_id
  WHERE {not_test}
    AND (%(notam_id)s::text IS NULL OR h.notam_id = %(notam_id)s)
    AND (%(locations)s::text[] IS NULL OR h.location = ANY(%(locations)s))
    AND (%(sua_like)s::text IS NULL OR (h.geometry_source = 'SUA_REFERENCE'
         AND h.hazard_id LIKE %(sua_like)s))
    AND (%(center)s::text IS NULL OR ST_DWithin(h.geom::geography,
         ST_GeomFromEWKT(%(center)s)::geography, %(radius_m)s))
    AND (h.hazard_class = 'airspace'
         OR (%(include_obstacles)s AND h.hazard_class = 'obstacle'))
    AND {hazard_type}
    AND (h.effective_start IS NULL
         OR h.effective_start <= %(now)s + make_interval(mins => %(horizon)s))
    AND (h.effective_end IS NULL OR h.effective_end >= %(now)s)
  LIMIT %(max_hazards)s
),
fl AS (
  SELECT f.flight_key, f.callsign, f.origin, f.destination, f.observed_at,
    f.geom, f.altitude_ft, f.ground_speed_kts, f.track_heading_deg,
    f.aircraft_type, f.user_category, r.geom AS route,
    r.planned_altitude_ft, ST_Length(r.geom::geography) AS route_len_m,
    greatest(coalesce(f.ground_speed_kts, 0), 120) * 1852
      * %(horizon)s / 60.0 AS ahead_m
  FROM aviation.flights f
  LEFT JOIN aviation.flight_routes r ON r.flight_ref = f.route_flight_ref
  WHERE f.observed_at >= %(now)s - make_interval(mins => %(fresh)s)
),
cand AS (
  SELECT fl.*, hz.*,
    CASE WHEN fl.route IS NOT NULL THEN
      ST_LineLocatePoint(fl.route, fl.geom) END AS frac
  FROM fl JOIN hz
    -- Cheap degree bounding-box prefilter (1 deg >= 33 km of longitude
    -- up to ~72N) so the exact geography test only runs on nearby
    -- pairs; without it a nationwide scan exceeds the 30 s timeout.
    ON fl.geom && ST_Expand(hz.hgeom, fl.ahead_m / 33000.0)
    AND ST_DWithin(fl.geom::geography, hz.hgeom::geography, fl.ahead_m)
),
paths AS (
  SELECT c.*,
    CASE
      WHEN c.route IS NOT NULL AND c.frac < 0.999
        AND ST_DWithin(c.geom::geography, c.route::geography, 20 * 1852)
      THEN ST_AddPoint(ST_LineSubstring(c.route, c.frac,
             least(1.0, c.frac + c.ahead_m / nullif(c.route_len_m, 0))),
           c.geom, 0)
      WHEN c.track_heading_deg IS NOT NULL
      THEN ST_MakeLine(c.geom, ST_Project(c.geom::geography, c.ahead_m,
             radians(c.track_heading_deg))::geometry)
    END AS path,
    CASE
      WHEN c.route IS NOT NULL AND c.frac < 0.999
        AND ST_DWithin(c.geom::geography, c.route::geography, 20 * 1852)
      THEN 'planned_route'
      WHEN c.track_heading_deg IS NOT NULL THEN 'track_extrapolation'
    END AS projection_basis
  FROM cand c
),
hits AS (
  SELECT p.*, ST_Intersects(p.geom, p.hgeom) AS inside_now,
    (SELECT min(ST_LineLocatePoint(p.path, d.geom))
     FROM ST_DumpPoints(ST_Intersection(p.path, p.hgeom)) d) AS entry_frac,
    ST_Length(p.path::geography) AS path_len_m,
    ST_Distance(p.geom::geography, p.hgeom::geography) / 1852
      AS distance_now_nm
  FROM paths p
  WHERE p.path IS NOT NULL
    AND (ST_Intersects(p.path, p.hgeom) OR ST_Intersects(p.geom, p.hgeom))
)
SELECT *, ST_X(geom) AS lon, ST_Y(geom) AS lat
FROM hits
ORDER BY inside_now DESC, entry_frac * path_len_m
LIMIT 1000
