-- NOTAMs in effect at a time, filtered by location, text, Q-code prefix
-- and/or distance from a point. Distance uses the NOTAM's mapped hazard area
-- when it has one, else its anchor point (FDC TFRs are anchored at the
-- issuing ARTCC, often far from the restricted area).
--
-- pt       the search point, if any.
-- near_hz  NOTAMs with a hazard area within the radius, and how close.
--
-- Params: codes, text (ILIKE pattern), q (LIKE pattern), near (EWKT point),
--   radius_m, at, include_obstacles, batch_size, offset.
WITH pt AS (
  SELECT ST_GeomFromEWKT(%(near)s)::geography AS g
  WHERE %(near)s::text IS NOT NULL
),
near_hz AS (
  SELECT h.notam_version_id,
    min(ST_Distance(h.geom::geography, pt.g)) / 1852 AS d
  FROM aviation.notam_hazards h, pt
  WHERE ST_DWithin(h.geom::geography, pt.g, %(radius_m)s)
  GROUP BY h.notam_version_id
)
SELECT n.notam_id, n.location, n.category AS q_code, n.effective_start,
  n.effective_end, left(n.raw_text, 400) AS text, n.hazard_count,
  n.anchor_source, n.schedule, ST_X(n.anchor) AS lon, ST_Y(n.anchor) AS lat,
  CASE
    WHEN %(near)s::text IS NULL THEN NULL
    WHEN coalesce(n.hazard_count, 0) > 0 THEN nh.d
    ELSE ST_Distance(n.anchor::geography, (SELECT g FROM pt)) / 1852
  END AS distance_nm,
  CASE WHEN coalesce(n.hazard_count, 0) > 0 THEN 'hazard_area'
       ELSE 'anchor_point' END AS distance_basis
FROM aviation.notams n
LEFT JOIN near_hz nh ON nh.notam_version_id = n.notam_version_id
WHERE {not_test}
  AND (%(codes)s::text[] IS NULL OR n.location = ANY(%(codes)s))
  AND (%(text)s::text IS NULL OR n.raw_text ILIKE %(text)s)
  AND (%(q)s::text IS NULL OR n.category LIKE %(q)s)
  AND (%(near)s::text IS NULL
       OR nh.notam_version_id IS NOT NULL
       OR (coalesce(n.hazard_count, 0) = 0
           AND ST_DWithin(n.anchor::geography, (SELECT g FROM pt),
                          %(radius_m)s)))
  AND (n.effective_start IS NULL OR n.effective_start <= %(at)s)
  AND (n.effective_end IS NULL OR n.effective_end >= %(at)s)
  AND (%(include_obstacles)s OR (coalesce(n.category, '') NOT LIKE 'QOB%%'
       AND coalesce(n.raw_text, '') NOT LIKE 'OBST %%'))
ORDER BY distance_nm NULLS LAST, n.effective_start DESC NULLS LAST,
         n.notam_version_id
LIMIT %(batch_size)s OFFSET %(offset)s
