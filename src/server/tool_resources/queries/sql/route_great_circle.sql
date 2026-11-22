-- Route basis, fallback: the great circle between the two airports, densified
-- every 50 km so it follows the curve on a lat/lon map.
-- Params: olon/olat, dlon/dlat (airport points).
WITH l AS (SELECT ST_Segmentize(ST_MakeLine(
    ST_SetSRID(ST_MakePoint(%(olon)s, %(olat)s), 4326),
    ST_SetSRID(ST_MakePoint(%(dlon)s, %(dlat)s), 4326))::geography,
    50000)::geometry AS g)
SELECT ST_AsEWKT(g) AS wkt, ST_Length(g::geography) / 1852 AS length_nm
FROM l
