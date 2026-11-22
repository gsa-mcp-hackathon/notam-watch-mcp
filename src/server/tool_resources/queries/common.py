"""Shared pieces for the gold-layer queries: constants, the SQL loader,
altitude/time/schedule helpers and the data_status block every tool returns.

Distances are nautical miles; altitudes are feet. Hazard altitude bands in AGL
remain unresolved because terrain elevation is not modeled.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from server.constants import (  # noqa: F401 -- re-exported
    CLIMB_DESCENT_FT_PER_NM,
    DEFAULT_CRUISE_FT,
    DEFAULT_SPEED_KTS,
    HAZARD_TYPES,
    HOUR,
    MAX_WATCH_HAZARDS,
    METERS_PER_NM,
    MIN_PLANNED_CRUISE_FT,
    NATIONWIDE_HAZARD_TYPES,
    TERMINAL_RADIUS_NM,
    VERTICAL_BUFFER_FT,
)
from server.models import Fetch  # noqa: F401 -- re-exported
from server.tool_resources.constants import (  # noqa: F401 -- re-exported
    HAZARD_TYPE_SQL,
    SQL_FRAGMENTS,
)
from server.tool_resources.services.notam_schedule import (
    NotamSchedule,
    is_scheduled,
    is_scheduled_during,
    parse_schedule,
    schedule_text,
)
from server.utils import (  # noqa: F401 -- re-exported
    iso,
    parse_time,
    rounded,
)


def hazard_type_predicate(hazard_type: str | None) -> str:
    """SQL predicate for a hazard_type, TRUE when None."""
    if hazard_type is None:
        return "TRUE"
    key = hazard_type.strip().lower()
    if key not in HAZARD_TYPE_SQL:
        raise ValueError(
            f"hazard_type must be one of {', '.join(HAZARD_TYPES)}; got {hazard_type!r}"
        )
    return HAZARD_TYPE_SQL[key]


# -------------------------------------------------------------- SQL files

_SQL_DIR = Path(__file__).with_name("sql")


@lru_cache(maxsize=None)
def _sql_file(name: str) -> str:
    return (_SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def sql(name: str, **fragments: str) -> str:
    """The query in ``sql/<name>.sql`` with its {fragment} slots filled from
    SQL_FRAGMENTS plus ``fragments`` (e.g. hazard_type)."""
    return _sql_file(name).format_map({**SQL_FRAGMENTS, **fragments})


# ---------------------------------------------------------------- helpers


def altitude_band(hazard: dict[str, Any]) -> tuple[float | None, float | None]:
    """(lower, upper) feet; SFC -> 0, UNBOUNDED -> inf, unknown -> None."""
    lower_ref = (hazard.get("lower_altitude_reference") or "").upper()
    upper_ref = (hazard.get("upper_altitude_reference") or "").upper()
    lower = hazard.get("lower_altitude_ft")
    upper = hazard.get("upper_altitude_ft")
    if lower_ref == "SFC":
        lower = 0.0
    if upper_ref == "UNBOUNDED":
        upper = math.inf
    return (
        None if lower is None else float(lower),
        None if upper is None else float(upper),
    )


def band_text(hazard: dict[str, Any]) -> str:
    lower, upper = altitude_band(hazard)

    def side(value: float | None, ref: str) -> str:
        if value is None:
            return "unknown"
        if value == 0 and ref == "SFC":
            return "SFC"
        if math.isinf(value):
            return "unlimited"
        if ref == "STD":
            return f"FL{int(value // 100):03d}"
        if ref in ("", "UNKNOWN"):
            return f"{int(value):,} ft (reference unknown)"
        return f"{int(value):,} ft {ref}"

    return (
        f"{side(lower, (hazard.get('lower_altitude_reference') or '').upper())}"
        f" to "
        f"{side(upper, (hazard.get('upper_altitude_reference') or '').upper())}"
    )


def vertical_relation(
    altitude_ft: float | None, hazard: dict[str, Any]
) -> dict[str, Any]:
    """Where an aircraft altitude sits relative to a hazard's band.

    ``relation`` is overlaps / above / below / unknown. AGL bounds cannot be
    compared with an MSL aircraft altitude without terrain elevation, so they
    remain unknown rather than being treated as numerically equivalent.
    """
    if hazard.get("hazard_class") in ("aerodrome", "navaid"):
        return {
            "band": "none published (facility/feature NOTAM)",
            "approximate": False,
            "relation": "not_applicable",
        }
    lower, upper = altitude_band(hazard)
    refs = {
        (hazard.get("lower_altitude_reference") or "").upper(),
        (hazard.get("upper_altitude_reference") or "").upper(),
    }
    result: dict[str, Any] = {
        "band": band_text(hazard),
        "approximate": False,
    }
    if "AGL" in refs:
        result["relation"] = "unknown"
        result["unresolved_reason"] = "AGL band requires terrain elevation"
        return result
    if altitude_ft is None or (lower is None and upper is None):
        result["relation"] = "unknown"
        return result
    low = altitude_ft - VERTICAL_BUFFER_FT
    high = altitude_ft + VERTICAL_BUFFER_FT
    floor = 0.0 if lower is None else lower
    if high < floor:
        result["relation"] = "below"
    elif upper is None:
        result["relation"] = "unknown"  # ceiling not published
    elif low > upper:
        result["relation"] = "above"
    else:
        result["relation"] = "overlaps"
    return result


def expected_altitude(
    along_nm: float,
    total_nm: float,
    *,
    start_ft: float,
    cruise_ft: float,
    end_ft: float,
) -> float:
    """Simple climb / cruise / descent profile along a route."""
    cruise = max(cruise_ft, start_ft)
    climb = start_ft + max(along_nm, 0.0) * CLIMB_DESCENT_FT_PER_NM
    descent = end_ft + max(total_nm - along_nm, 0.0) * CLIMB_DESCENT_FT_PER_NM
    return max(min(cruise, climb, descent), 0.0)


def flight_phase(
    along_nm: float, total_nm: float, altitude: float, cruise: float
) -> str:
    if altitude >= cruise - 500:
        return "cruise"
    return "climb" if along_nm < total_nm / 2 else "descent"


def is_active(hazard: dict[str, Any], at: datetime) -> bool:
    """Inside the NOTAM's effective window (ignores the daily schedule)."""
    start = hazard.get("effective_start")
    end = hazard.get("effective_end")
    return (start is None or start <= at) and (end is None or end >= at)


@lru_cache(maxsize=4096)
def _parsed_schedule(text: str, effective_start: datetime | None) -> NotamSchedule:
    return parse_schedule(text, effective_start)


def applies_at(row: dict[str, Any], at: datetime) -> bool | None:
    """Effective window AND item-D schedule (e.g. 'DLY 1230-2330',
    'MON-FRI', 'SR-SS'). None: in window but the schedule could not be
    evaluated, so the NOTAM may apply."""
    if not is_active(row, at):
        return False
    text = row.get("schedule")
    if not text:
        return True
    return is_scheduled(
        _parsed_schedule(text, row.get("effective_start")),
        at,
        lat=row.get("hz_lat") if row.get("hz_lat") is not None else row.get("lat"),
        lon=row.get("hz_lon") if row.get("hz_lon") is not None else row.get("lon"),
    )


def applies_during(
    row: dict[str, Any], window_start: datetime, window_end: datetime
) -> bool | None:
    """Whether a NOTAM applies at any point in a time window.

    ``None`` preserves schedules that are present but cannot be evaluated.
    """
    effective_start = row.get("effective_start")
    effective_end = row.get("effective_end")
    if effective_end is not None and effective_end < window_start:
        return False
    if effective_start is not None and effective_start > window_end:
        return False
    start = max(window_start, effective_start) if effective_start else window_start
    end = min(window_end, effective_end) if effective_end else window_end
    text = row.get("schedule")
    if not text:
        return True
    return is_scheduled_during(
        _parsed_schedule(text, effective_start),
        start,
        end,
        lat=row.get("hz_lat") if row.get("hz_lat") is not None else row.get("lat"),
        lon=row.get("hz_lon") if row.get("hz_lon") is not None else row.get("lon"),
    )


def hazard_summary(row: dict[str, Any]) -> dict[str, Any]:
    """The NOTAM facts of a row selected with {hazard_fields}."""
    position = None
    if row.get("hz_lat") is not None and row.get("hz_lon") is not None:
        position = {
            "lat": rounded(row.get("hz_lat"), 4),
            "lon": rounded(row.get("hz_lon"), 4),
        }
    return {
        "notam_id": row.get("notam_id"),
        "location": row.get("location"),
        "q_code": row.get("q_code"),
        "hazard_class": row.get("hazard_class"),
        "geometry_source": row.get("geometry_source"),
        "effective_start": iso(row.get("effective_start")),
        "effective_end": iso(row.get("effective_end")) or "open-ended",
        "parse_confidence": rounded(row.get("parse_confidence"), 2),
        "position": position,
        "position_basis": "point on mapped hazard area" if position else None,
        "schedule": schedule_text(row.get("schedule")),
        "text": row.get("text"),
    }


# ------------------------------------------------------------ data status


def data_status(fetch: Fetch) -> dict[str, Any]:
    """Gold publish time and live-flight lag, included in every response."""
    rows = fetch(sql("data_status"), {})
    row = rows[0] if rows else {}
    flights_as_of = row.get("flights_as_of")
    lag = None
    if flights_as_of is not None:
        lag = round((datetime.now(UTC) - flights_as_of).total_seconds() / 60, 1)
    return {
        "gold_published_at": iso(row.get("published_at")),
        "flight_positions_as_of": iso(flights_as_of),
        "flight_data_lag_minutes": lag,
        "note": (
            "Flight times are relative to flight_positions_as_of. A large lag "
            "means the live feed is paused; treat flight results as a replay."
        ),
    }


def flights_now(fetch: Fetch) -> datetime:
    """Time of the newest flight position (falls back to the clock)."""
    rows = fetch(sql("flights_now"), {})
    return (rows[0]["t"] if rows and rows[0].get("t") else None) or datetime.now(UTC)
