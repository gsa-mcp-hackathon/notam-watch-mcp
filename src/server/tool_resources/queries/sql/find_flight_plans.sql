-- Filed TFMS plans matching a callsign and departure window, with any latest
-- live position attached. Params: cs, filter_origin, origins,
-- filter_destination, destinations, window_start, window_end, target, limit.
SELECT r.flight_ref, r.gufi, r.callsign, r.origin, r.destination,
  r.route_string, r.departure_procedure, r.arrival_procedure,
  r.etd_type, r.etd_time, r.eta_type, r.eta_time,
  r.planned_altitude_ft, r.filed_true_airspeed_kts,
  r.centers, r.revision_count, r.observed_at AS plan_observed_at,
  live.position_time, live.altitude_ft, live.ground_speed_kts,
  live.aircraft_type,
  CASE WHEN live.position_time IS NULL THEN false ELSE true END
    AS has_live_position,
  CASE WHEN r.etd_time IS NULL THEN NULL ELSE
    EXTRACT(EPOCH FROM (r.etd_time - %(target)s)) / 60 END
    AS departure_offset_minutes
FROM aviation.flight_routes r
LEFT JOIN LATERAL (
  SELECT f.observed_at AS position_time, f.altitude_ft,
    f.ground_speed_kts, f.aircraft_type
  FROM aviation.flights f
  WHERE f.flight_ref = r.flight_ref OR f.route_flight_ref = r.flight_ref
  ORDER BY f.observed_at DESC
  LIMIT 1
) live ON true
WHERE upper(r.callsign) = %(cs)s
  AND (NOT %(filter_origin)s OR upper(r.origin) = ANY(%(origins)s))
  AND (NOT %(filter_destination)s OR
       upper(r.destination) = ANY(%(destinations)s))
  AND (
    r.etd_time BETWEEN %(window_start)s AND %(window_end)s
    OR (
      r.etd_time IS NULL
      AND r.observed_at BETWEEN %(window_start)s - interval '24 hours'
                            AND %(window_end)s
    )
  )
ORDER BY
  CASE WHEN r.etd_time IS NULL THEN 1 ELSE 0 END,
  abs(EXTRACT(EPOCH FROM (r.etd_time - %(target)s))),
  r.observed_at DESC
LIMIT %(limit)s
