"""get_flights_approaching_notams: live flights inside, or about to enter,
active NOTAM airspace.

1. One spatial query (sql/flights_approaching_hazards.sql) projects every
   fresh flight along its planned route or current track and finds the
   hazard areas it is in or will enter within the horizon.
2. Per flight x area: merge NOTAMs that publish the identical area, drop
   entries outside the NOTAM's schedule or clearly above/below its altitude
   band, and label the rest confirmed or altitude_unknown.
3. For special use airspace, add whether entry falls in its published times.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from server.tool_resources.queries.common import (
    MAX_WATCH_HAZARDS,
    METERS_PER_NM,
    NATIONWIDE_HAZARD_TYPES,
    Fetch,
    applies_at,
    flights_now,
    hazard_summary,
    hazard_type_predicate,
    iso,
    rounded,
    sql,
    vertical_relation,
)
from server.tool_resources.services.sua_schedule import published_times_status


def flights_approaching_hazards(
    *,
    notam_id: str | None,
    center_lon: float | None,
    center_lat: float | None,
    radius_nm: float,
    horizon_minutes: int,
    include_obstacles: bool,
    limit: int,
    fetch: Fetch,
    only_vertical_conflicts: bool = True,
    freshness_minutes: int = 10,
    notam_location: str | None = None,
    sua_designator: str | None = None,
    hazard_type: str | None = None,
) -> dict[str, Any]:
    """Flights inside, or projected to enter within the horizon, active NOTAM
    airspace hazard areas. Projection follows the remaining planned route when the
    flight is within 20 NM of it, otherwise extrapolates the current track.

    ``notam_location`` disambiguates a notam_id (domestic numbers repeat per
    location); ``sua_designator`` (e.g. R5206) watches the NOTAM activations
    of one special use airspace; ``hazard_type`` (tfr, sua, uas, warning, any)
    restricts to one hazard family and, with no other anchor, scans the whole
    country in one call."""
    type_sql = hazard_type_predicate(hazard_type)
    nationwide = not any(
        (notam_id, notam_location, sua_designator, center_lon is not None)
    )
    if nationwide and (
        hazard_type is None
        or hazard_type.strip().lower() not in NATIONWIDE_HAZARD_TYPES
    ):
        raise ValueError(
            "a nationwide scan needs hazard_type "
            f"{', '.join(NATIONWIDE_HAZARD_TYPES)}; for uas/any (or no "
            "hazard_type) also give an airport or latitude/longitude"
        )
    now = flights_now(fetch)
    locations = None
    if notam_location:
        code = notam_location.strip().upper()
        other = code[1:] if len(code) == 4 and code[0] == "K" else "K" + code
        locations = [code, other]
    center = (
        f"SRID=4326;POINT({center_lon} {center_lat})"
        if center_lon is not None and center_lat is not None
        else None
    )
    rows = fetch(
        sql("flights_approaching_hazards", hazard_type=type_sql),
        {
            "notam_id": notam_id,
            "locations": locations,
            "sua_like": f"sua:%:{sua_designator}" if sua_designator else None,
            "center": center,
            "radius_m": radius_nm * METERS_PER_NM,
            "include_obstacles": include_obstacles,
            "now": now,
            "horizon": horizon_minutes,
            "fresh": freshness_minutes,
            "max_hazards": MAX_WATCH_HAZARDS,
        },
    )
    # (flight, identical hazard area) -> encounter. Every NOTAM is assessed
    # before grouping because schedules and altitude bands can differ even
    # when the published geometry is identical.
    grouped: dict[tuple[Any, Any], dict[str, Any]] = {}
    merged_duplicates = 0
    vertically_clear = 0
    outside_schedule = 0
    sua_encounters: list[tuple[dict[str, Any], str, datetime]] = []
    for r in rows:
        flight_id = r.get("flight_key") or r.get("callsign")
        area_key = (flight_id, r.get("geom_key") or r.get("hazard_id"))
        speed = max(float(r.get("ground_speed_kts") or 0), 60.0)
        entry_nm = (
            None
            if r.get("inside_now") or r.get("entry_frac") is None
            else float(r["entry_frac"]) * float(r["path_len_m"]) / METERS_PER_NM
        )
        entry_at = now + timedelta(hours=(entry_nm or 0.0) / speed)
        scheduled = applies_at(r, entry_at)
        if scheduled is False:
            outside_schedule += 1
            continue
        vertical = vertical_relation(r.get("altitude_ft"), r)
        planned = vertical_relation(r.get("planned_altitude_ft"), r)
        if only_vertical_conflicts and not (
            vertical["relation"] in ("overlaps", "unknown")
            or planned["relation"] == "overlaps"
        ):
            vertically_clear += 1
            continue
        certainty = (
            "confirmed"
            if "overlaps" in (vertical["relation"], planned["relation"])
            else "altitude_unknown"
        )
        evidence = {
            **hazard_summary(r),
            "notam_applies_at_entry": scheduled,
            "vertical_relation_current_altitude": vertical["relation"],
            "vertical_relation_planned_altitude": planned["relation"],
            "vertical_is_approximate": (
                vertical["approximate"] or planned["approximate"]
            ),
            "vertical_unresolved_reason": (
                vertical.get("unresolved_reason")
                or planned.get("unresolved_reason")
            ),
            "hazard_altitude_band": vertical["band"],
            "altitude_certainty": certainty,
        }
        encounter = grouped.get(area_key)
        if encounter is None:
            encounter = {
                "callsign": r["callsign"],
                "aircraft_type": r.get("aircraft_type"),
                "user_category": r.get("user_category"),
                "origin": r.get("origin"),
                "destination": r.get("destination"),
                "position": {"lat": rounded(r["lat"], 4), "lon": rounded(r["lon"], 4)},
                "position_time": iso(r["observed_at"]),
                "altitude_ft": rounded(r.get("altitude_ft"), 0),
                "ground_speed_kts": rounded(r.get("ground_speed_kts"), 0),
                "status": "inside_now" if r.get("inside_now") else "projected_entry",
                "distance_now_nm": rounded(r.get("distance_now_nm")),
                "distance_to_entry_nm": rounded(entry_nm),
                "minutes_to_entry": (
                    None if entry_nm is None else rounded(entry_nm / speed * 60)
                ),
                "projection_basis": r.get("projection_basis"),
                "_notam_evidence": [evidence],
                "_sua_refs": {},
            }
            grouped[area_key] = encounter
        else:
            encounter["_notam_evidence"].append(evidence)
            merged_duplicates += 1
        if r.get("geometry_source") == "SUA_REFERENCE" and r.get("hazard_id"):
            designator = str(r["hazard_id"]).rsplit(":", 1)[-1]
            encounter["_sua_refs"][designator] = entry_at

    encounters = []
    for encounter in grouped.values():
        evidence = encounter.pop("_notam_evidence")
        primary = max(
            evidence,
            key=lambda item: (
                item["altitude_certainty"] == "confirmed",
                item["notam_applies_at_entry"] is True,
                float(item.get("parse_confidence") or 0),
            ),
        )
        encounter.update(
            {
                "notam_applies_at_entry": primary["notam_applies_at_entry"],
                "vertical_relation_current_altitude": primary[
                    "vertical_relation_current_altitude"
                ],
                "vertical_relation_planned_altitude": primary[
                    "vertical_relation_planned_altitude"
                ],
                "vertical_is_approximate": primary["vertical_is_approximate"],
                "vertical_unresolved_reason": primary[
                    "vertical_unresolved_reason"
                ],
                "hazard_altitude_band": primary["hazard_altitude_band"],
                "altitude_certainty": primary["altitude_certainty"],
                "hazard": {
                    key: primary[key]
                    for key in (
                        "notam_id",
                        "location",
                        "q_code",
                        "hazard_class",
                        "geometry_source",
                        "effective_start",
                        "effective_end",
                        "parse_confidence",
                        "schedule",
                        "text",
                    )
                },
            }
        )
        encounter["hazard"]["same_area_notams"] = [
            f"{item.get('notam_id')} {item.get('location')}"
            for item in evidence
            if item is not primary
        ]
        encounter["hazard"]["same_area_notam_evidence"] = evidence
        sua_refs = encounter.pop("_sua_refs")
        sua_encounters.extend(
            (encounter, designator, entry_at)
            for designator, entry_at in sua_refs.items()
        )
        encounters.append(encounter)
    _attach_sua_published_times(sua_encounters, fetch)
    # Confirmed vertical conflicts first; within each group keep the SQL order
    # (inside now, then soonest entry). sort() is stable.
    encounters.sort(key=lambda e: e["altitude_certainty"] != "confirmed")
    by_certainty = {
        c: [e for e in encounters if e["altitude_certainty"] == c]
        for c in ("confirmed", "altitude_unknown")
    }
    return {
        "evaluated_at": iso(now),
        "horizon_minutes": horizon_minutes,
        "scope": {
            "hazard_type": hazard_type.strip().lower() if hazard_type else None,
            "area": "nationwide" if nationwide else "filtered",
            "flights_affected": len({e["callsign"] for e in encounters}),
        },
        "counts": {
            c: {
                "encounters": len(es),
                "flights": len({e["callsign"] for e in es}),
            }
            for c, es in by_certainty.items()
        },
        "merged_duplicate_area_encounters": merged_duplicates,
        "total_encounters": len(encounters),
        "excluded_vertically_clear": vertically_clear,
        "excluded_outside_notam_schedule": outside_schedule,
        "encounters": encounters[:limit],
        "assumptions": [
            "Constant ground speed; planned-route projection when the flight is "
            "within 20 NM of its TFMS route, else straight-line track extrapolation.",
            "Vertical relation uses +/-1,000 ft around the altitude. AGL bands "
            "remain unresolved without terrain elevation and are not excluded.",
            "With only_vertical_conflicts, flights clearly above/below the NOTAM "
            "band at both current and planned altitude are excluded (counted in "
            "excluded_vertically_clear).",
            "NOTAMs with a daily/weekday schedule (e.g. 'DLY 1230-2330') only "
            "count when the entry time falls inside it; notam_applies_at_entry "
            "null means the schedule could not be evaluated.",
            "Separate NOTAMs with an identical area count once per flight only "
            "after each NOTAM's schedule and altitude band are evaluated. Per-NOTAM "
            "facts are retained in hazard.same_area_notam_evidence.",
            "altitude_certainty 'confirmed' means the current or planned altitude "
            "overlaps the NOTAM band; 'altitude_unknown' means the band could not "
            "be parsed, so the flight may be well above or below it. Report the "
            "two counts separately; confirmed encounters are listed first.",
            "For NOTAM-activated special use airspace, "
            "hazard.sua_published_times says whether the entry time is inside "
            "the airspace's published (charted, local-time) times of use. The "
            "NOTAM's own effective time governs; this is context only.",
        ],
    }


def _attach_sua_published_times(
    items: list[tuple[dict[str, Any], str, datetime]], fetch: Fetch
) -> None:
    """Add each SUA encounter's published times of use at its entry time
    (one query for all designators)."""
    if not items:
        return
    rows = fetch(
        sql("sua_published_times"),
        {"designators": sorted({designator for _, designator, _ in items})},
    )
    by_designator = {row["designator"]: row for row in rows}
    for encounter, designator, entry_at in items:
        sua = by_designator.get(designator)
        if sua is None:
            continue
        encounter["hazard"]["sua_published_times"] = {
            "designator": designator,
            **published_times_status(
                sua.get("schedule"), entry_at, lat=sua.get("lat"), lon=sua.get("lon")
            ),
        }
