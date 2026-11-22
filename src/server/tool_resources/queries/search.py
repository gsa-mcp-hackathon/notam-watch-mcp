"""search_notams: direct NOTAM lookup by location, text, Q-code and/or
distance from a point (sql/search_notams.sql)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.tool_resources.queries.common import (
    METERS_PER_NM,
    Fetch,
    applies_at,
    iso,
    rounded,
    sql,
)
from server.tool_resources.services.notam_schedule import schedule_text


def search_notams(
    *,
    location: str | None,
    text_contains: str | None,
    q_code_prefix: str | None,
    near_lon: float | None,
    near_lat: float | None,
    radius_nm: float,
    active_at: datetime,
    include_obstacles: bool,
    limit: int,
    fetch: Fetch,
) -> dict[str, Any]:
    """Find NOTAMs by airport/location, text, Q-code and/or proximity.

    Proximity uses the NOTAM's mapped hazard area when it has one, else its
    anchor point. FDC NOTAMs (TFRs) are anchored at the issuing ARTCC, often
    hundreds of miles from the restricted area, so the anchor alone would
    miss them.
    """
    codes = None
    if location:
        ident = location.strip().upper()
        codes = [ident]
        if len(ident) == 4 and ident.startswith("K"):
            codes.append(ident[1:])
        elif len(ident) == 3:
            codes.append("K" + ident)
    near = (
        f"SRID=4326;POINT({near_lon} {near_lat})"
        if near_lon is not None and near_lat is not None
        else None
    )
    params = {
        "codes": codes,
        "text": f"%{text_contains.strip()}%" if text_contains else None,
        "q": f"{q_code_prefix.strip().upper()}%" if q_code_prefix else None,
        "near": near,
        "radius_m": radius_nm * METERS_PER_NM,
        "at": active_at,
        "include_obstacles": include_obstacles,
    }
    rows: list[dict[str, Any]] = []
    batch_size = 250
    offset = 0
    while True:
        batch = fetch(
            sql("search_notams"),
            {**params, "batch_size": batch_size, "offset": offset},
        )
        rows.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size

    applicable = []
    excluded_outside_schedule = 0
    unresolved_schedules = 0
    for row in rows:
        schedule_state = applies_at(row, active_at)
        if schedule_state is False:
            excluded_outside_schedule += 1
            continue
        if schedule_state is None:
            unresolved_schedules += 1
        applicable.append((row, schedule_state))
    return {
        "active_at": iso(active_at),
        "total_matches": len(applicable),
        "excluded_outside_schedule": excluded_outside_schedule,
        "unresolved_schedules_in_matches": unresolved_schedules,
        "notams": [
            {
                "notam_id": r["notam_id"],
                "location": r["location"],
                "q_code": r["q_code"],
                "effective_start": iso(r["effective_start"]),
                "effective_end": iso(r["effective_end"]) or "open-ended",
                "has_mapped_hazard_area": (r.get("hazard_count") or 0) > 0,
                "position": {"lat": rounded(r.get("lat"), 4),
                             "lon": rounded(r.get("lon"), 4)}
                if r.get("lat") is not None and r.get("lon") is not None
                else None,
                "position_basis": "NOTAM anchor point",
                "distance_nm": rounded(r.get("distance_nm")),
                "distance_basis": r.get("distance_basis")
                if r.get("distance_nm") is not None
                else None,
                "schedule": schedule_text(r.get("schedule")),
                "applies_at_active_at": schedule_state,
                "text": r["text"],
            }
            for r, schedule_state in applicable[:limit]
        ],
    }
