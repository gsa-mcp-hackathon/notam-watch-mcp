"""find_route_deviations_near_notams: flights off their filed route near
active NOTAM airspace (possible avoidance or reroutes).

One query (sql/route_deviations.sql) finds flights more than
min_deviation_nm from their TFMS plan and, for each, the nearest active
airspace NOTAM at a relevant altitude and whether the plan crosses it.
"""

from __future__ import annotations

from typing import Any

from server.tool_resources.queries.common import (
    METERS_PER_NM,
    Fetch,
    applies_at,
    band_text,
    flights_now,
    hazard_summary,
    iso,
    rounded,
    sql,
    vertical_relation,
)


def route_deviations(
    *,
    min_deviation_nm: float,
    near_notam_nm: float,
    require_nearby_notam: bool,
    limit: int,
    fetch: Fetch,
    freshness_minutes: int = 10,
) -> dict[str, Any]:
    """Flights currently off their TFMS planned route, with the nearest
    active airspace NOTAM at a relevant altitude and whether the plan itself
    crosses it."""
    now = flights_now(fetch)
    rows = fetch(
        sql("route_deviations"),
        {
            "now": now,
            "fresh": freshness_minutes,
            "min_dev_m": min_deviation_nm * METERS_PER_NM,
            "near_m": near_notam_nm * METERS_PER_NM,
            "require_notam": require_nearby_notam,
            "limit": limit,
        },
    )
    flights = []
    for r in rows:
        entry: dict[str, Any] = {
            "callsign": r["callsign"],
            "aircraft_type": r.get("aircraft_type"),
            "origin": r.get("origin"),
            "destination": r.get("destination"),
            "position": {"lat": rounded(r["lat"], 4), "lon": rounded(r["lon"], 4)},
            "position_time": iso(r["observed_at"]),
            "altitude_ft": rounded(r.get("altitude_ft"), 0),
            "cross_track_deviation_nm": rounded(r.get("cross_nm")),
            "beyond_end_of_planned_route": float(r.get("frac") or 0) >= 0.999,
            "route_string": r.get("route_string"),
        }
        if r.get("hazard_id"):
            vertical = vertical_relation(r.get("altitude_ft"), r)
            entry["nearest_active_notam"] = {
                **hazard_summary(r),
                "distance_nm": rounded(r.get("hazard_distance_nm")),
                "planned_route_crosses_it": bool(r.get("plan_crosses_hazard")),
                "altitude_band": band_text(r),
                "vertical_relation": vertical["relation"],
                "vertical_unresolved_reason": vertical.get("unresolved_reason"),
                "applies_now": applies_at(r, now),
            }
        flights.append(entry)
    return {
        "evaluated_at": iso(now),
        "min_deviation_nm": min_deviation_nm,
        "total_deviating_flights": int(rows[0]["total"]) if rows else 0,
        "flights": flights,
        "interpretation": (
            "A deviating flight whose planned route crosses a nearby active "
            "NOTAM area may be avoiding it (or being rerouted around it). "
            "beyond_end_of_planned_route=true usually means the modeled route "
            "ended (e.g., oceanic), not a real deviation."
        ),
    }
