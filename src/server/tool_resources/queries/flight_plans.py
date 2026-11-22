"""Find filed flight plans by callsign and approximate departure time."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from server.tool_resources.queries.common import Fetch, iso, rounded, sql


def _airport_variants(code: str | None) -> list[str]:
    if not code:
        return []
    ident = code.strip().upper()
    if len(ident) == 4 and ident.startswith("K"):
        return [ident, ident[1:]]
    if len(ident) == 3:
        return [ident, f"K{ident}"]
    return [ident]


def find_flight_plans(
    *,
    callsign: str,
    origin: str | None,
    destination: str | None,
    departure_time: datetime,
    window_minutes: int,
    limit: int,
    fetch: Fetch,
) -> dict[str, Any]:
    """Return candidate TFMS plans close to a requested departure time."""
    origins = _airport_variants(origin)
    destinations = _airport_variants(destination)
    window_start = departure_time - timedelta(minutes=window_minutes)
    window_end = departure_time + timedelta(minutes=window_minutes)
    rows = fetch(
        sql("find_flight_plans"),
        {
            "cs": callsign.strip().upper(),
            "filter_origin": bool(origins),
            "origins": origins,
            "filter_destination": bool(destinations),
            "destinations": destinations,
            "window_start": window_start,
            "window_end": window_end,
            "target": departure_time,
            "limit": limit,
        },
    )
    plans = [
        {
            "callsign": row.get("callsign"),
            "flight_ref": row.get("flight_ref"),
            "gufi": row.get("gufi"),
            "origin": row.get("origin"),
            "destination": row.get("destination"),
            "etd": iso(row.get("etd_time")),
            "eta": iso(row.get("eta_time")),
            "departure_offset_minutes": rounded(
                row.get("departure_offset_minutes"), 0
            ),
            "planned_altitude_ft": row.get("planned_altitude_ft"),
            "filed_true_airspeed_kts": row.get("filed_true_airspeed_kts"),
            "route_string": row.get("route_string"),
            "departure_procedure": row.get("departure_procedure"),
            "arrival_procedure": row.get("arrival_procedure"),
            "centers": row.get("centers"),
            "route_revisions": row.get("revision_count"),
            "plan_observed_at": iso(row.get("plan_observed_at")),
            "has_live_position": bool(row.get("has_live_position")),
            "live_position_time": iso(row.get("position_time")),
            "current_altitude_ft": row.get("altitude_ft"),
            "current_ground_speed_kts": row.get("ground_speed_kts"),
            "aircraft_type": row.get("aircraft_type"),
        }
        for row in rows
    ]
    return {
        "query": {
            "callsign": callsign.strip().upper(),
            "origin": origin.strip().upper() if origin else None,
            "destination": destination.strip().upper() if destination else None,
            "departure_time": iso(departure_time),
            "window_minutes": window_minutes,
        },
        "matches": len(plans),
        "plans": plans,
        "next_step": (
            "Use the selected plan's origin, destination, ETD, planned altitude, "
            "and callsign with get_route_notams."
            if plans
            else "No matching TFMS plan is currently available. Do not infer an "
            "airline schedule or destination from the callsign alone."
        ),
    }
