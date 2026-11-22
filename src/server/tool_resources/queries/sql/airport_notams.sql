-- Aerodrome NOTAMs (runways, ILS, approaches, ...) at one airport that are in
-- effect at some point in a time window, most operationally relevant
-- Q-codes first (airport_priority).
-- Params: codes (FAA and ICAO ids), t_start, t_end, batch_size, offset.
SELECT n.notam_id, n.location, n.category AS q_code, n.effective_start,
       n.effective_end, left(n.raw_text, 300) AS text, n.schedule
FROM aviation.notams n
WHERE n.location = ANY(%(codes)s) AND {not_test}
  AND (n.effective_start IS NULL OR n.effective_start <= %(t_end)s)
  AND (n.effective_end IS NULL OR n.effective_end >= %(t_start)s)
ORDER BY coalesce(array_position({airport_priority}, left(n.category, 3)),
                  99), n.effective_start DESC NULLS LAST, n.notam_version_id
LIMIT %(batch_size)s OFFSET %(offset)s
