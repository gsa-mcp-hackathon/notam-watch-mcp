"""Airport lookup and aerodrome NOTAMs (runways, ILS, approaches, ...)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.tool_resources.queries.common import Fetch, applies_during, iso, sql
from server.tool_resources.services.notam_schedule import schedule_text


def resolve_airport(code: str, fetch: Fetch) -> dict[str, Any] | None:
    """Look up an airport by ICAO (KJFK) or FAA (JFK) identifier."""
    ident = code.strip().upper()
    faa = ident[1:] if len(ident) == 4 and ident.startswith("K") else ident
    rows = fetch(sql("airport"), {"ident": ident, "faa": faa})
    return rows[0] if rows else None


def airport_codes(airport: dict[str, Any]) -> list[str]:
    """The airport's FAA and ICAO identifiers (NOTAMs use either)."""
    return [c for c in {airport.get("arpt_id"), airport.get("icao_id")} if c]


def airport_notams(
    airport: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    fetch: Fetch,
    limit: int = 20,
) -> dict[str, Any]:
    """Aerodrome NOTAMs (runways, ILS, approaches, ...) active in a window."""
    params = {
        "codes": airport_codes(airport),
        "t_start": window_start,
        "t_end": window_end,
    }
    rows: list[dict[str, Any]] = []
    batch_size = 250
    offset = 0
    while True:
        batch = fetch(
            sql("airport_notams"),
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
        located = {**row, "lat": airport.get("lat"), "lon": airport.get("lon")}
        schedule_state = applies_during(located, window_start, window_end)
        if schedule_state is False:
            excluded_outside_schedule += 1
            continue
        if schedule_state is None:
            unresolved_schedules += 1
        applicable.append((located, schedule_state))
    return {
        "airport": airport.get("icao_id") or airport.get("arpt_id"),
        "name": airport.get("name"),
        "window": [iso(window_start), iso(window_end)],
        "total_active": len(applicable),
        "excluded_outside_schedule": excluded_outside_schedule,
        "unresolved_schedules_in_active": unresolved_schedules,
        "notams": [
            {
                "notam_id": r["notam_id"],
                "q_code": r["q_code"],
                "effective_start": iso(r["effective_start"]),
                "effective_end": iso(r["effective_end"]) or "open-ended",
                **_airport_schedule(r, schedule_state),
                "text": r["text"],
            }
            for r, schedule_state in applicable[:limit]
        ],
    }


def _airport_schedule(
    row: dict[str, Any],
    schedule_state: bool | None,
) -> dict[str, Any]:
    """Schedule evidence across the complete airport time window."""
    if not row.get("schedule"):
        return {}
    return {
        "schedule": schedule_text(row["schedule"]),
        "schedule_applies_during_window": schedule_state,
    }
