"""get_route_notams: which NOTAMs matter on a flight from A to B.

1. Pick a route line (this flight's plan, a recent filed route for the city
   pair, or the great circle).
2. Find hazard areas in a corridor along it (sql/hazards_along_route.sql).
3. Estimate when and at what altitude the flight passes each one, and
   whether it is in effect and vertically relevant then.
4. Add departure and arrival airport NOTAMs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from server.tool_resources.queries.airports import (
    airport_codes,
    airport_notams,
    resolve_airport,
)
from server.tool_resources.queries.common import (
    CLIMB_DESCENT_FT_PER_NM,
    DEFAULT_CRUISE_FT,
    DEFAULT_SPEED_KTS,
    HOUR,
    METERS_PER_NM,
    MIN_PLANNED_CRUISE_FT,
    TERMINAL_RADIUS_NM,
    VERTICAL_BUFFER_FT,
    Fetch,
    applies_at,
    expected_altitude,
    flight_phase,
    hazard_summary,
    iso,
    rounded,
    sql,
    vertical_relation,
)


def _route_basis(
    origin: dict[str, Any],
    destination: dict[str, Any],
    callsign: str | None,
    fetch: Fetch,
) -> dict[str, Any]:
    """Pick the best route line: this flight's plan, a recent filed route for
    the same city pair, or the great circle."""
    if callsign:
        rows = fetch(sql("route_by_callsign"), {"cs": callsign.strip().upper()})
        if rows:
            return {**rows[0], "source": "flight_plan"}
    points = {
        "olon": origin["lon"],
        "olat": origin["lat"],
        "dlon": destination["lon"],
        "dlat": destination["lat"],
    }
    rows = fetch(
        sql("route_by_city_pair"),
        {"o": airport_codes(origin), "d": airport_codes(destination), **points},
    )
    if rows:
        return {**rows[0], "source": "recent_filed_route_same_city_pair"}
    rows = fetch(sql("route_great_circle"), points)
    return {**rows[0], "source": "great_circle"}


def hazards_along_line(
    route_wkt: str,
    *,
    corridor_nm: float,
    window_start: datetime,
    window_end: datetime,
    include_obstacles: bool,
    fetch: Fetch,
    classes: tuple[str, ...] = ("airspace", "navaid"),
    max_rows: int = 300,
) -> list[dict[str, Any]]:
    """Hazard shapes of the given classes within ``corridor_nm`` of a route
    line and active at some point in the window, with off-route distance and
    along-route position of the closest approach. Obstacles are added near
    the route ends (or everywhere when include_obstacles)."""
    return fetch(
        sql("hazards_along_route"),
        {
            "route": route_wkt,
            "corridor_m": corridor_nm * METERS_PER_NM,
            "terminal_m": TERMINAL_RADIUS_NM * METERS_PER_NM,
            "t_start": window_start,
            "t_end": window_end,
            "include_obstacles": include_obstacles,
            "classes": list(classes),
            "max_rows": max_rows,
        },
    )


def assess_route_hazards(
    hazards: list[dict[str, Any]],
    *,
    route_length_nm: float,
    departure_time: datetime,
    speed_kts: float,
    start_altitude_ft: float,
    cruise_altitude_ft: float,
    end_altitude_ft: float,
) -> list[dict[str, Any]]:
    """Add estimated pass time, altitude, phase and overlap facts per hazard.

    The model, not this function, decides which hazards matter; results are
    only ordered so the strongest conflicts come first.
    """
    assessed = []
    for row in hazards:
        along = float(row.get("along_route_nm") or 0.0)
        altitude = expected_altitude(
            along,
            route_length_nm,
            start_ft=start_altitude_ft,
            cruise_ft=cruise_altitude_ft,
            end_ft=end_altitude_ft,
        )
        passing_at = departure_time + timedelta(hours=along / max(speed_kts, 60.0))
        vertical = vertical_relation(altitude, row)
        active = applies_at(row, passing_at)
        if active is False:
            continue
        crosses = bool(row.get("route_crosses"))
        assessed.append(
            {
                **hazard_summary(row),
                "route_crosses_hazard": crosses,
                "off_route_nm": rounded(row.get("off_route_nm")),
                "along_route_nm": rounded(along),
                "estimated_pass_time": iso(passing_at),
                "active_at_estimated_pass_time": active,
                "estimated_altitude_ft": int(round(altitude, -2)),
                "flight_phase": flight_phase(
                    along, route_length_nm, altitude, cruise_altitude_ft
                ),
                "hazard_altitude_band": vertical["band"],
                "vertical_relation": vertical["relation"],
                "vertical_is_approximate": vertical["approximate"],
                "vertical_unresolved_reason": vertical.get("unresolved_reason"),
                "_score": (
                    (4 if crosses else 0)
                    + (2 if vertical["relation"] in ("overlaps", "unknown") else 0)
                    + (2 if active else 1 if active is None else 0)
                    + (1 if row.get("hazard_class") == "airspace" else 0)
                    - float(row.get("off_route_nm") or 0) / 100
                ),
            }
        )
    assessed.sort(key=lambda item: item["_score"], reverse=True)
    for item in assessed:
        item.pop("_score")
    return assessed


def route_notams(
    *,
    origin_code: str,
    destination_code: str,
    departure_time: datetime | None,
    cruise_altitude_ft: float | None,
    corridor_nm: float,
    callsign: str | None,
    include_obstacles: bool,
    limit: int,
    fetch: Fetch,
) -> dict[str, Any]:
    """Evidence for 'which NOTAMs matter on my flight from A to B?'."""
    origin = resolve_airport(origin_code, fetch)
    destination = resolve_airport(destination_code, fetch)
    missing = [
        code
        for code, found in ((origin_code, origin), (destination_code, destination))
        if not found
    ]
    if missing:
        return {"error": f"Unknown airport identifier(s): {', '.join(missing)}."}

    departure = departure_time or datetime.now(UTC)
    basis = _route_basis(origin, destination, callsign, fetch)
    length_nm = float(basis["length_nm"])
    speed = float(basis.get("filed_true_airspeed_kts") or DEFAULT_SPEED_KTS)
    planned = float(basis.get("planned_altitude_ft") or 0)
    cruise = float(
        cruise_altitude_ft
        or (planned if planned >= MIN_PLANNED_CRUISE_FT else DEFAULT_CRUISE_FT)
    )
    arrival = departure + timedelta(hours=length_nm / speed)
    hazards = hazards_along_line(
        basis["wkt"],
        corridor_nm=corridor_nm,
        window_start=departure - timedelta(minutes=30),
        window_end=arrival + timedelta(minutes=30),
        include_obstacles=include_obstacles,
        fetch=fetch,
    )
    assessed = assess_route_hazards(
        hazards,
        route_length_nm=length_nm,
        departure_time=departure,
        speed_kts=speed,
        start_altitude_ft=float(origin.get("elevation_ft") or 0),
        cruise_altitude_ft=cruise,
        end_altitude_ft=float(destination.get("elevation_ft") or 0),
    )
    obstacles = [h for h in assessed if h["hazard_class"] == "obstacle"]
    enroute = [h for h in assessed if h["hazard_class"] != "obstacle"]
    return {
        "route": {
            "origin": origin.get("icao_id") or origin.get("arpt_id"),
            "destination": destination.get("icao_id") or destination.get("arpt_id"),
            "origin_position": {
                "lat": rounded(origin.get("lat"), 4),
                "lon": rounded(origin.get("lon"), 4),
            },
            "destination_position": {
                "lat": rounded(destination.get("lat"), 4),
                "lon": rounded(destination.get("lon"), 4),
            },
            "basis": basis["source"],
            "example_flight": basis.get("callsign"),
            "route_string": basis.get("route_string"),
            "length_nm": rounded(length_nm),
            "assumed_cruise_altitude_ft": int(cruise),
            "assumed_speed_kts": int(speed),
            "departure_time": iso(departure),
            "estimated_arrival_time": iso(arrival),
            "corridor_nm": corridor_nm,
        },
        "enroute_hazards": {
            "total_in_corridor": len(enroute),
            "by_class": {
                cls: sum(1 for h in enroute if h["hazard_class"] == cls)
                for cls in ("airspace", "navaid")
            },
            "crossed_by_route": sum(1 for h in enroute if h["route_crosses_hazard"]),
            "vertical_overlap_and_active": sum(
                1
                for h in enroute
                if h["vertical_relation"] in ("overlaps", "unknown")
                and h["active_at_estimated_pass_time"] is not False
            ),
            "hazards": enroute[:limit],
        },
        "obstacles": {
            "total": len(obstacles),
            "scope": "whole corridor"
            if include_obstacles
            else f"within {int(TERMINAL_RADIUS_NM)} NM of departure/arrival",
            "vertical_overlap": sum(
                1 for h in obstacles if h["vertical_relation"] == "overlaps"
            ),
            "items": obstacles[: min(limit, 10)],
        },
        "departure_airport": airport_notams(
            origin, departure - HOUR, departure + HOUR, fetch
        ),
        "arrival_airport": airport_notams(
            destination, arrival - HOUR, arrival + HOUR, fetch
        ),
        "assumptions": [
            f"Altitude profile: climb/descent {int(CLIMB_DESCENT_FT_PER_NM)} ft/NM "
            f"to {int(cruise)} ft; vertical buffer +/-{int(VERTICAL_BUFFER_FT)} ft.",
            "AGL hazard bands remain vertically unresolved because terrain "
            "elevation is not available; they are not excluded as vertically clear.",
            "Obstacle NOTAMs are included only within "
            f"{int(TERMINAL_RADIUS_NM)} NM of the departure/arrival airports "
            "unless include_obstacles is true.",
            "Aerodrome NOTAMs (runway, taxiway, lighting, ILS) are listed under "
            "departure_airport/arrival_airport; navaid outages near the route "
            "appear as hazard_class=navaid with no altitude band.",
        ],
    }
