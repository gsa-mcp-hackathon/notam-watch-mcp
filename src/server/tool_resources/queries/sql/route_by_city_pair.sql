-- Route basis, second choice: a recent filed route between the same two
-- airports whose ends lie within 30 NM of them, preferring one with a route
-- string.
-- Params: o, d (airport id lists), olon/olat, dlon/dlat (airport points).
SELECT {route_columns} FROM aviation.flight_routes r
WHERE r.origin = ANY(%(o)s) AND r.destination = ANY(%(d)s)
  AND ST_DWithin(ST_StartPoint(r.geom)::geography,
                 ST_SetSRID(ST_MakePoint(%(olon)s, %(olat)s), 4326)::geography,
                 30 * 1852)
  AND ST_DWithin(ST_EndPoint(r.geom)::geography,
                 ST_SetSRID(ST_MakePoint(%(dlon)s, %(dlat)s), 4326)::geography,
                 30 * 1852)
ORDER BY (r.route_string IS NOT NULL) DESC, r.observed_at DESC
LIMIT 1
