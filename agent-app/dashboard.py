"""MCP-backed dashboard data and map-view normalization."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastmcp import Client

ALLOWED_TOOLS = {
    "get_flights_approaching_notams",
    "get_flight_briefing",
    "find_route_deviations_near_notams",
    "get_route_notams",
    "search_notams",
}

TFR_WATCH_TOOL = "get_flights_approaching_notams"
TFR_WATCH_ARGUMENTS = {"hazard_type": "tfr", "horizon_minutes": 60, "limit": 40}


def parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _result_data(result: Any) -> dict[str, Any]:
    if isinstance(result.data, dict):
        return result.data
    structured = result.structured_content
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for block in result.content or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _call_tool(
    mcp_url: str, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if name not in ALLOWED_TOOLS:
        return {"tool": name, "error": "Unsupported dashboard tool."}
    client = Client(mcp_url, auth=token, timeout=120, mode="legacy")
    try:
        async with client:
            result = await client.call_tool(name, arguments, timeout=120)
    except Exception as exc:  # noqa: BLE001 - return partial dashboard evidence
        return {"tool": name, "arguments": arguments, "error": str(exc)}
    return {"tool": name, "arguments": arguments, "data": _result_data(result)}


async def startup_briefing(mcp_url: str, token: str) -> dict[str, Any]:
    result = await _call_tool(mcp_url, token, TFR_WATCH_TOOL, TFR_WATCH_ARGUMENTS)
    return normalize_results([result], "National TFR watch")


async def default_dashboard(mcp_url: str, token: str) -> dict[str, Any]:
    calls = [
        (TFR_WATCH_TOOL, TFR_WATCH_ARGUMENTS),
        (
            "find_route_deviations_near_notams",
            {
                "min_deviation_nm": 5,
                "near_notam_nm": 30,
                "require_nearby_notam": True,
                "limit": 30,
            },
        ),
    ]
    results = await asyncio.gather(
        *(_call_tool(mcp_url, token, name, arguments) for name, arguments in calls)
    )
    return normalize_results(results, "National airspace watch")


async def dashboard_for_tool_calls(
    mcp_url: str,
    token: str,
    tool_calls: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for call in tool_calls[:3]:
        name = str(call.get("name") or "")
        if name not in ALLOWED_TOOLS:
            continue
        calls.append((name, parse_arguments(call.get("arguments"))))
    if not calls:
        return normalize_results([], query)
    results = await asyncio.gather(
        *(_call_tool(mcp_url, token, name, arguments) for name, arguments in calls)
    )
    return normalize_results(results, query)


def _position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        lat = float(value["lat"])
        lon = float(value["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


def _severity_rank(value: str) -> int:
    return {"critical": 4, "high": 3, "uncertain": 2, "advisory": 1}.get(
        value, 0
    )


def _route_points(coordinates: Any) -> list[dict[str, float]]:
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return []
    points = []
    for coordinate in coordinates:
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            return []
        lon, lat = coordinate
        point = _position({"lat": lat, "lon": lon})
        if point is None:
            return []
        points.append(point)
    return points


def _approach_severity(encounter: dict[str, Any]) -> str:
    if encounter.get("altitude_certainty") != "confirmed":
        return "uncertain"
    if encounter.get("status") == "inside_now":
        return "critical"
    minutes = encounter.get("minutes_to_entry")
    if isinstance(minutes, int | float) and minutes <= 15:
        return "critical"
    return "high"


def _hazard_severity(hazard: dict[str, Any]) -> str:
    relation = hazard.get("vertical_relation")
    active = hazard.get("active_at_estimated_pass_time")
    if relation == "overlaps" and active is not False:
        return "high"
    if relation == "unknown" and active is not False:
        return "uncertain"
    return "advisory"


def normalize_results(results: list[dict[str, Any]], title: str) -> dict[str, Any]:
    flights: dict[str, dict[str, Any]] = {}
    notams: dict[str, dict[str, Any]] = {}
    routes: list[dict[str, Any]] = []
    notes: list[str] = []
    errors: list[str] = []
    source_tools: list[str] = []
    evaluated_times: list[str] = []
    data_statuses: list[dict[str, Any]] = []

    def add_flight(item: dict[str, Any]) -> None:
        callsign = str(item.get("callsign") or "Unknown")
        current = flights.get(callsign)
        if current is None or _severity_rank(item["severity"]) > _severity_rank(
            current["severity"]
        ):
            flights[callsign] = item

    def add_notam(
        hazard: dict[str, Any],
        *,
        severity: str,
        callsign: str | None = None,
        status: str = "Relevant",
        tool: str,
    ) -> None:
        notam_id = str(hazard.get("notam_id") or "Unidentified notice")
        location = str(hazard.get("location") or "")
        key = f"{notam_id}|{location}"
        entry = notams.get(key)
        if entry is None:
            entry = {
                "key": key,
                "notam_id": notam_id,
                "location": location,
                "q_code": hazard.get("q_code"),
                "text": hazard.get("text") or "NOTAM text unavailable.",
                "severity": severity,
                "status": status,
                "altitude_band": hazard.get("hazard_altitude_band")
                or hazard.get("altitude_band"),
                "effective_end": hazard.get("effective_end"),
                "position": _position(hazard.get("position")),
                "position_basis": hazard.get("position_basis") or "hazard area",
                "callsigns": [],
                "source_tools": [tool],
            }
            notams[key] = entry
        elif _severity_rank(severity) > _severity_rank(entry["severity"]):
            entry["severity"] = severity
            entry["status"] = status
        if callsign and callsign not in entry["callsigns"]:
            entry["callsigns"].append(callsign)
        if tool not in entry["source_tools"]:
            entry["source_tools"].append(tool)

    for result in results:
        tool = str(result.get("tool") or "")
        if tool:
            source_tools.append(tool)
        if result.get("error"):
            errors.append(f"{tool}: {result['error']}")
            continue
        data = result.get("data") or {}
        if data.get("error"):
            errors.append(f"{tool}: {data['error']}")
            continue
        if data.get("evaluated_at"):
            evaluated_times.append(str(data["evaluated_at"]))
        if isinstance(data.get("data_status"), dict):
            data_statuses.append(data["data_status"])
        notes.extend(str(note) for note in data.get("assumptions") or [])

        if tool == "get_flights_approaching_notams":
            for encounter in data.get("encounters") or []:
                position = _position(encounter.get("position"))
                if position is None:
                    continue
                severity = _approach_severity(encounter)
                callsign = str(encounter.get("callsign") or "Unknown")
                hazard = encounter.get("hazard") or {}
                add_flight(
                    {
                        "callsign": callsign,
                        "position": position,
                        "altitude_ft": encounter.get("altitude_ft"),
                        "origin": encounter.get("origin"),
                        "destination": encounter.get("destination"),
                        "status": "Inside hazard"
                        if encounter.get("status") == "inside_now"
                        else "Projected entry",
                        "detail": encounter.get("hazard_altitude_band"),
                        "minutes_to_entry": encounter.get("minutes_to_entry"),
                        "severity": severity,
                        "notam_id": hazard.get("notam_id"),
                        "source_tool": tool,
                    }
                )
                add_notam(
                    hazard,
                    severity=severity,
                    callsign=callsign,
                    status="Inside now"
                    if encounter.get("status") == "inside_now"
                    else "Projected encounter",
                    tool=tool,
                )

        elif tool == "find_route_deviations_near_notams":
            for item in data.get("flights") or []:
                position = _position(item.get("position"))
                if position is None:
                    continue
                hazard = item.get("nearest_active_notam") or {}
                severity = (
                    "high" if hazard.get("planned_route_crosses_it") else "advisory"
                )
                callsign = str(item.get("callsign") or "Unknown")
                add_flight(
                    {
                        "callsign": callsign,
                        "position": position,
                        "altitude_ft": item.get("altitude_ft"),
                        "origin": item.get("origin"),
                        "destination": item.get("destination"),
                        "status": "Off planned route",
                        "detail": (
                            f"{item.get('cross_track_deviation_nm') or '?'} NM off plan"
                        ),
                        "severity": severity,
                        "notam_id": hazard.get("notam_id"),
                        "source_tool": tool,
                    }
                )
                if hazard:
                    add_notam(
                        hazard,
                        severity=severity,
                        callsign=callsign,
                        status="Near deviating flight",
                        tool=tool,
                    )

        elif tool == "get_flight_briefing":
            flight = data.get("flight") or {}
            plan = data.get("plan") or {}
            position = _position(flight.get("position"))
            callsign = str(flight.get("callsign") or "Unknown")
            points = _route_points(plan.get("route_coordinates"))
            if points:
                routes.append({"label": f"{callsign} filed route", "points": points})
            if position:
                add_flight(
                    {
                        "callsign": callsign,
                        "position": position,
                        "altitude_ft": flight.get("altitude_ft"),
                        "origin": flight.get("origin"),
                        "destination": flight.get("destination"),
                        "status": "Selected flight",
                        "detail": plan.get("cross_track_deviation_nm"),
                        "severity": "advisory",
                        "source_tool": tool,
                    }
                )
            for hazard in data.get("hazards_ahead") or []:
                add_notam(
                    hazard,
                    severity=_hazard_severity(hazard),
                    callsign=callsign,
                    status="Ahead on remaining route",
                    tool=tool,
                )

        elif tool == "get_route_notams":
            route = data.get("route") or {}
            start = _position(route.get("origin_position"))
            end = _position(route.get("destination_position"))
            if start and end:
                routes.append(
                    {
                        "label": f"{route.get('origin')} to {route.get('destination')}",
                        "points": [start, end],
                    }
                )
            for hazard in (data.get("enroute_hazards") or {}).get("hazards") or []:
                add_notam(
                    hazard,
                    severity=_hazard_severity(hazard),
                    status="Along selected route",
                    tool=tool,
                )

        elif tool == "search_notams":
            for hazard in data.get("notams") or []:
                severity = (
                    "high"
                    if hazard.get("has_mapped_hazard_area")
                    else "advisory"
                )
                add_notam(
                    hazard,
                    severity=severity,
                    status="Matched search",
                    tool=tool,
                )

    sorted_notams = sorted(
        notams.values(),
        key=lambda item: (
            -_severity_rank(item["severity"]),
            -len(item["callsigns"]),
            item["notam_id"],
        ),
    )
    sorted_flights = sorted(
        flights.values(),
        key=lambda item: (-_severity_rank(item["severity"]), item["callsign"]),
    )
    severity_counts = defaultdict(int)
    for item in sorted_notams:
        severity_counts[item["severity"]] += 1
    freshness = next(
        (
            status
            for status in data_statuses
            if status.get("flight_positions_as_of")
        ),
        data_statuses[0] if data_statuses else {},
    )
    return {
        "title": title or "Analyst view",
        "generated_at": datetime.now(UTC).isoformat(),
        "evaluated_at": max(evaluated_times) if evaluated_times else None,
        "data_status": freshness,
        "source_tools": list(dict.fromkeys(source_tools)),
        "metrics": {
            "critical_notams": severity_counts["critical"],
            "high_notams": severity_counts["high"],
            "uncertain_notams": severity_counts["uncertain"],
            "flights_shown": len(sorted_flights),
            "notams_shown": len(sorted_notams),
        },
        "flights": sorted_flights,
        "notams": sorted_notams,
        "routes": routes,
        "notes": list(dict.fromkeys(notes))[:5],
        "errors": errors,
    }
