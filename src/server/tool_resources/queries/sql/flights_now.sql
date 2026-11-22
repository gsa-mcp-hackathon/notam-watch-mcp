-- "Now" for flight questions: the newest live position, so results stay
-- consistent when the feed lags or is replayed.
SELECT max(observed_at) AS t FROM aviation.flights
