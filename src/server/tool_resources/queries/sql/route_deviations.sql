-- Live flights more than min_dev_m off their TFMS planned route, each with
-- the nearest active airspace NOTAM that is vertically relevant to its
-- altitude (+/-1000 ft), or vertically unresolved, and whether the planned
-- route crosses that NOTAM.
--
-- dev   deviating flights (distance from position to planned route).
-- nh    per flight, the closest relevant hazard (LATERAL, one row), areas the
--       plan crosses first.
--
-- Params: now, fresh (minutes), min_dev_m, near_m, require_notam, limit.
WITH dev AS (
  SELECT f.flight_key, f.callsign, f.origin, f.destination, f.observed_at,
    f.geom, f.altitude_ft, f.ground_speed_kts, f.aircraft_type,
    r.geom AS route, r.route_string,
    ST_Distance(f.geom::geography, r.geom::geography) / 1852 AS cross_nm,
    ST_LineLocatePoint(r.geom, f.geom) AS frac
  FROM aviation.flights f
  JOIN aviation.flight_routes r ON r.flight_ref = f.route_flight_ref
  WHERE f.observed_at >= %(now)s - make_interval(mins => %(fresh)s)
    AND NOT ST_DWithin(f.geom::geography, r.geom::geography, %(min_dev_m)s)
)
SELECT dev.*, ST_X(dev.geom) AS lon, ST_Y(dev.geom) AS lat, nh.*,
  count(*) OVER () AS total
FROM dev
LEFT JOIN LATERAL (
  SELECT {hazard_fields},
    ST_Distance(h.geom::geography, dev.geom::geography) / 1852
      AS hazard_distance_nm,
    ST_Intersects(h.geom, dev.route) AS plan_crosses_hazard
  FROM aviation.notam_hazards h
  JOIN aviation.notams n ON n.notam_version_id = h.notam_version_id
  WHERE h.hazard_class = 'airspace' AND {not_test}
    AND (h.effective_start IS NULL OR h.effective_start <= %(now)s)
    AND (h.effective_end IS NULL OR h.effective_end >= %(now)s)
    AND ST_DWithin(h.geom::geography, dev.geom::geography, %(near_m)s)
    -- AGL cannot be compared with the MSL flight altitude without terrain;
    -- retain it as unresolved instead of excluding it numerically.
    AND (dev.altitude_ft IS NULL OR h.lower_altitude_reference IN ('SFC', 'AGL')
         OR h.lower_altitude_ft IS NULL
         OR h.lower_altitude_ft - 1000 <= coalesce(dev.altitude_ft, 0))
    AND (dev.altitude_ft IS NULL
         OR h.upper_altitude_reference IN ('UNBOUNDED', 'AGL')
         OR h.upper_altitude_ft IS NULL
         OR h.upper_altitude_ft + 1000 >= coalesce(dev.altitude_ft, 0))
  ORDER BY ST_Intersects(h.geom, dev.route) DESC,
           ST_Distance(h.geom::geography, dev.geom::geography)
  LIMIT 1
) nh ON true
WHERE NOT %(require_notam)s OR nh.hazard_id IS NOT NULL
ORDER BY (nh.plan_crosses_hazard IS TRUE) DESC, dev.cross_nm DESC
LIMIT %(limit)s
