-- Freshness of the gold layer: last publish and newest live flight position.
-- Used by every tool response (data_status).
SELECT (SELECT max(published_at) FROM aviation.publish_log) AS published_at,
       (SELECT max(observed_at) FROM aviation.flights) AS flights_as_of
