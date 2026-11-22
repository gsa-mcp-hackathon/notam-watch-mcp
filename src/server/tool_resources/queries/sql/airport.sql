-- One airport by ICAO (KJFK) or FAA (JFK) identifier, preferring an exact
-- ICAO match and airports over heliports/seaplane bases.
-- Params: ident (as typed, upper case), faa (ident without the K prefix).
SELECT arpt_id, icao_id, name, city, state_code, elevation_ft,
       resp_artcc_id, ST_X(geom) AS lon, ST_Y(geom) AS lat
FROM aviation.airports
WHERE icao_id = %(ident)s OR arpt_id = %(ident)s OR arpt_id = %(faa)s
ORDER BY (icao_id = %(ident)s) DESC, (site_type_code = 'A') DESC
LIMIT 1
