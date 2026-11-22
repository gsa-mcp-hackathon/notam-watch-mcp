"""get_flight_briefing: everything about one live flight by callsign.

Position, aircraft type facts, TFMS plan and how far the flight is off it
(sql/flight_briefing.sql), NOTAM hazards along the rest of the plan (reusing
the route-corridor search) and destination airport NOTAMs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from server.tool_resources.queries.airports import (
    airport_notams,
    resolve_airport,
)
from server.tool_resources.queries.common import (
    DEFAULT_SPEED_KTS,
    Fetch,
    iso,
    rounded,
    sql,
)
from server.tool_resources.queries.route_notams import (
    assess_route_hazards,
    hazards_along_line,
)


def flight_briefing(
    *, callsign: str, corridor_nm: float, limit: int, fetch: Fetch
) -> dict[str, Any]:
    """Current state, aircraft, plan conformance and NOTAMs ahead for one
    flight."""
    rows = fetch(sql("flight_briefing"), {"cs": callsign.strip().upper()})
    if not rows:
        return {
            "error": (
                f"No current position for {callsign.upper()}. The flight may not "
                "be airborne yet, may have landed, or uses a different callsign."
            )
        }
    f = rows[0]
    observed = f["observed_at"]
    result: dict[str, Any] = {
        "flight": {
            "callsign": f["callsign"],
            "origin": f.get("origin"),
            "destination": f.get("destination"),
            "position": {"lat": rounded(f["lat"], 4), "lon": rounded(f["lon"], 4)},
            "position_time": iso(observed),
            "altitude_ft": rounded(f.get("altitude_ft"), 0),
            "ground_speed_kts": rounded(f.get("ground_speed_kts"), 0),
            "track_heading_deg": rounded(f.get("track_heading_deg"), 0),
            "user_category": f.get("user_category"),
            "track_points_last_3h": f.get("point_count"),
        },
        "aircraft": {
            "type": f.get("aircraft_type"),
            "heavy": f.get("special_aircraft_qualifier"),
            "manufacturer": f.get("manufacturer"),
            "model": f.get("model"),
            "engine_class": f.get("engine_class") or f.get("aircraft_engine_class"),
            "approach_category": f.get("aircraft_approach_category"),
            "design_group": f.get("airplane_design_group"),
            "wingspan_ft": f.get("wingspan_ft"),
            "length_ft": f.get("length_ft"),
            "tail_height_ft": f.get("tail_height_ft"),
            "max_takeoff_weight_lb": f.get("mtow_lb"),
            "wake_category": f.get("recat_category") or f.get("icao_wtc"),
            "note": (
                "Dimensions/weights are type maximums from the FAA Aircraft "
                "Characteristics Database, not this airframe's actual values."
            )
            if f.get("model")
            else "Aircraft type unknown or not in the reference table.",
        },
    }
    if f.get("route_len_nm") is None:
        result["plan"] = {"available": False}
        result["hazards_ahead"] = []
    else:
        remaining_nm = float(f.get("remaining_nm") or 0)
        result["plan"] = {
            "available": True,
            "route_string": f.get("route_string"),
            "route_coordinates": f.get("route_coordinates"),
            "departure_procedure": f.get("departure_procedure"),
            "arrival_procedure": f.get("arrival_procedure"),
            "etd": iso(f.get("etd_time")),
            "eta": iso(f.get("eta_time")),
            "planned_altitude_ft": f.get("planned_altitude_ft"),
            "centers": f.get("centers"),
            "route_revisions": f.get("revision_count"),
            "remaining_nm": rounded(remaining_nm),
            "cross_track_deviation_nm": rounded(f.get("cross_track_nm")),
            "conformance_note": (
                "Cross-track deviation is the distance from the current position "
                "to the TFMS planned route; above ~5 NM suggests a reroute, "
                "weather/NOTAM avoidance, or vectoring. Near the end of an "
                "oceanic route it can reflect the end of the modeled path."
            ),
        }
        hazards = []
        if f.get("remaining_wkt") and remaining_nm > 1:
            speed = float(f.get("ground_speed_kts") or DEFAULT_SPEED_KTS)
            arrival = observed + timedelta(hours=remaining_nm / max(speed, 60.0))
            hazards = assess_route_hazards(
                hazards_along_line(
                    f["remaining_wkt"],
                    corridor_nm=corridor_nm,
                    window_start=observed,
                    window_end=arrival + timedelta(minutes=30),
                    include_obstacles=False,
                    fetch=fetch,
                ),
                route_length_nm=remaining_nm,
                departure_time=observed,
                speed_kts=speed,
                start_altitude_ft=float(f.get("altitude_ft") or 0),
                cruise_altitude_ft=float(
                    f.get("planned_altitude_ft") or f.get("altitude_ft") or 0
                ),
                end_altitude_ft=0.0,
            )
        result["hazards_ahead"] = hazards[:limit]
    destination = f.get("destination")
    airport = resolve_airport(destination, fetch) if destination else None
    if airport:
        eta = f.get("eta_time") or observed + timedelta(hours=1)
        result["destination_airport"] = airport_notams(
            airport, eta - timedelta(hours=1), eta + timedelta(hours=1), fetch, 15
        )
    return result
