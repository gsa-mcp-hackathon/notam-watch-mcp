-- Published times of use (FAA SAA Timesheets) and a reference point for
-- several special use airspaces at once.
-- Params: designators (normalized, e.g. R5206).
SELECT designator, schedule,
  ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat
FROM aviation.sua_airspaces WHERE designator = ANY(%(designators)s)
