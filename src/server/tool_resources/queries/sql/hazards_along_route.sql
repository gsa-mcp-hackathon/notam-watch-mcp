-- NOTAM hazard areas within a corridor of a route line and in effect at some
-- point in a time window, with the off-route distance, the along-route
-- position of the closest approach and whether the route crosses the area.
-- Obstacles are only included near the route ends unless include_obstacles.
-- Params: route (EWKT line), corridor_m, terminal_m, t_start, t_end,
--   include_obstacles, classes (hazard classes to keep), max_rows.
WITH r AS (SELECT ST_GeomFromEWKT(%(route)s) AS g),
rg AS (SELECT g, g::geography AS gg, ST_Length(g::geography) AS len_m
       FROM r)
SELECT {hazard_fields},
  ST_Distance(h.geom::geography, rg.gg) / 1852 AS off_route_nm,
  ST_LineLocatePoint(rg.g, ST_ClosestPoint(rg.g, h.geom)) * rg.len_m / 1852
    AS along_route_nm,
  ST_Intersects(h.geom, rg.g) AS route_crosses
FROM aviation.notam_hazards h
JOIN aviation.notams n ON n.notam_version_id = h.notam_version_id
CROSS JOIN rg
WHERE ST_DWithin(h.geom::geography, rg.gg, %(corridor_m)s)
  AND {not_test}
  AND (h.effective_start IS NULL OR h.effective_start <= %(t_end)s)
  AND (h.effective_end IS NULL OR h.effective_end >= %(t_start)s)
  AND (h.hazard_class = ANY(%(classes)s)
       OR (h.hazard_class = 'obstacle' AND (%(include_obstacles)s
           OR ST_DWithin(h.geom::geography,
                         ST_StartPoint(rg.g)::geography, %(terminal_m)s)
           OR ST_DWithin(h.geom::geography,
                         ST_EndPoint(rg.g)::geography, %(terminal_m)s))))
ORDER BY route_crosses DESC, off_route_nm
LIMIT %(max_rows)s
