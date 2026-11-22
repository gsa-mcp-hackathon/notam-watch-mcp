-- Route basis, first choice: the latest TFMS planned route of this flight.
-- Params: cs (callsign).
SELECT {route_columns} FROM aviation.flight_routes
WHERE callsign = %(cs)s ORDER BY observed_at DESC LIMIT 1
