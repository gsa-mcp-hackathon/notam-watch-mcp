-- One special use airspace by designator (R5206) or name (LIVE OAK, or
-- LIVE OAK MOA), with its charted limits, published times of use and a
-- reference point.
-- Params: designator (letters and digits only), name (upper case).
SELECT designator, name, name_code, sua_type, lower_ft, lower_reference,
  upper_ft, upper_reference, schedule, nasr_effective_date,
  ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat
FROM aviation.sua_airspaces
WHERE designator = %(designator)s
   OR replace(upper(name_code), '''', '') = %(name)s
   OR replace(upper(name_code), '''', '') = %(name)s || ' MOA'
ORDER BY (designator = %(designator)s) DESC
LIMIT 1
