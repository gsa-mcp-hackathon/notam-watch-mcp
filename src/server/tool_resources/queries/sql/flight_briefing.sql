-- Everything about one live flight: latest position, aircraft type facts,
-- TFMS plan, distance off the plan, the remaining part of the plan and the
-- 3 h track summary.
-- Params: cs (callsign).
SELECT f.*, ST_X(f.geom) AS lon, ST_Y(f.geom) AS lat,
  a.manufacturer, a.model, a.engine_class, a.aircraft_approach_category,
  a.airplane_design_group, a.wingspan_ft, a.length_ft, a.tail_height_ft,
  a.mtow_lb, a.icao_wtc, a.faa_weight_class, a.recat_category,
  r.route_string, r.departure_procedure, r.arrival_procedure, r.etd_type,
  r.etd_time, r.eta_type, r.eta_time, r.planned_altitude_ft,
  r.filed_true_airspeed_kts, r.centers, r.revision_count,
  ST_AsGeoJSON(r.geom, 5)::json -> 'coordinates' AS route_coordinates,
  ST_Length(r.geom::geography) / 1852 AS route_len_nm,
  ST_Distance(f.geom::geography, r.geom::geography) / 1852 AS cross_track_nm,
  loc.frac,
  CASE WHEN loc.frac < 0.999 THEN
    ST_AsEWKT(ST_LineSubstring(r.geom, loc.frac, 1)) END AS remaining_wkt,
  CASE WHEN loc.frac < 0.999 THEN
    ST_Length(ST_LineSubstring(r.geom, loc.frac, 1)::geography) / 1852
  ELSE 0 END AS remaining_nm,
  t.point_count, t.first_observed_at AS track_start
FROM aviation.flights f
LEFT JOIN aviation.aircraft_types a ON a.icao_code = f.aircraft_type
LEFT JOIN aviation.flight_routes r ON r.flight_ref = f.route_flight_ref
LEFT JOIN aviation.flight_tracks t ON t.flight_key = f.flight_key
LEFT JOIN LATERAL (
  SELECT ST_LineLocatePoint(r.geom, f.geom) AS frac) loc ON true
WHERE f.callsign = %(cs)s
ORDER BY f.observed_at DESC
LIMIT 1
