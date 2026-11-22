"""Evaluate FAA special use airspace (SAA) published times of use.

The FAA SAA AIXM file gives each airspace Timesheets such as "MON-FRI
07:00-22:00" with ``timeReference=UTC`` but a ``timeOffset`` extension (e.g.
-5.0). The clock times are therefore *local* standard-offset times, so a
moment is converted to local time before matching. The file does not say
whether daylight saving applies:

- offsets of places that keep standard time all year (Puerto Rico -4,
  Hawaii -10, American Samoa -11, Guam +10) never shift;
- -7 is ambiguous (Arizona keeps standard time; the rest of Mountain time
  does not), so during US daylight saving both readings are tried and the
  result is None (unknown) when they disagree;
- every other offset follows US daylight saving.

Published times of use say when an airspace *may* be in use. Many SUAs are
only active when activated by NOTAM, so this is context, not a replacement
for the NOTAM's own effective time. Results are three-valued: True (inside
the published times), False (outside), None (cannot be determined).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from server.tool_resources.constants import (
    AMBIGUOUS_DST_OFFSETS,
    NO_DST_OFFSETS,
    PROHIBITED_DAY_VALUES,
    WEEKDAY_NAMES,
)
from server.tool_resources.services.notam_schedule import _sun_times


def month_day(value: str | None) -> str | None:
    """'MM-DD' from an AIXM date-of-year.

    Accepts the FAA SAA 5.0 form 'DD-MM' (e.g. '31-12') and the AIXM 5.1
    DateMonthDayType '--MM-DD'. Returns None if absent or invalid.
    """
    if not value:
        return None
    text = value.strip()
    if text.startswith("--"):
        match = re.fullmatch(r"--(\d{2})-(\d{2})", text)
        month, day = (match.group(1), match.group(2)) if match else (None, None)
    else:
        match = re.fullmatch(r"(\d{2})-(\d{2})", text)
        day, month = (match.group(1), match.group(2)) if match else (None, None)
    if not match:
        return None
    try:
        date(2024, int(month), int(day))  # leap year, so 02-29 is valid
    except ValueError:
        return None
    return f"{month}-{day}"


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def us_dst_in_effect(local_standard: datetime) -> bool:
    """US daylight saving (2nd Sunday of March 02:00 to 1st Sunday of
    November 02:00 daylight = 01:00 standard), on a naive local-standard time."""
    year = local_standard.year
    start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time()).replace(
        hour=2
    )
    end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time()).replace(
        hour=1
    )
    return start <= local_standard < end


def _minutes(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", (value or "").strip())
    if not match:
        return None
    total = int(match.group(1)) * 60 + int(match.group(2))
    return total if total <= 1440 else None


def _md(sheet: dict[str, Any], key: str) -> str | None:
    return sheet.get(f"{key}_md") or month_day(sheet.get(f"{key}_date"))


def _date_ok(sheet: dict[str, Any], day: date) -> bool | None:
    raw = (sheet.get("start_date"), sheet.get("end_date"))
    start, end = _md(sheet, "start"), _md(sheet, "end")
    if not start or not end:
        return None if any(raw) else True
    md = f"{day.month:02d}-{day.day:02d}"
    return start <= md <= end if start <= end else (md >= start or md <= end)


def _weekday_ok(sheet: dict[str, Any], day: date) -> bool | None:
    first = (sheet.get("day") or "ANY").upper()
    last = (sheet.get("day_til") or "").upper()
    if first in PROHIBITED_DAY_VALUES or last in PROHIBITED_DAY_VALUES:
        return None  # WORK_DAY / HOL etc. cannot be evaluated
    if first == "ANY":
        return True
    if first not in WEEKDAY_NAMES or (last and last not in WEEKDAY_NAMES):
        return None
    return day.weekday() == WEEKDAY_NAMES.index(first)


def _edge(
    sheet: dict[str, Any],
    side: str,
    day: date,
    shift_hr: float,
    lat: float | None,
    lon: float | None,
) -> int | None:
    """Minutes after local midnight for the sheet's start or end on ``day``."""
    clock = _minutes(sheet.get(f"{side}_time"))
    if clock is not None:
        return 1440 if side == "end" and clock == 0 else clock
    event = (sheet.get(f"{side}_event") or "").upper()
    if event not in ("SR", "SS") or lat is None or lon is None:
        return None
    sun = _sun_times(day, lat, lon)
    if sun is None:
        return None
    moment = (sun[0] if event == "SR" else sun[1]) + timedelta(hours=shift_hr)
    minutes = moment.hour * 60 + moment.minute + (moment.date() - day).days * 1440
    try:
        minutes += int(sheet.get(f"{side}_event_offset_min") or 0)
    except (TypeError, ValueError):
        return None
    return minutes


def _session_span_days(sheet: dict[str, Any]) -> int:
    """Days from ``day`` to ``dayTil`` when the sheet is one continuous
    session (e.g. WED 06:00 to THU 02:00); 0 for a daily window."""
    first = (sheet.get("day") or "").upper()
    last = (sheet.get("day_til") or "").upper()
    if first in WEEKDAY_NAMES and last in WEEKDAY_NAMES and first != last:
        return (WEEKDAY_NAMES.index(last) - WEEKDAY_NAMES.index(first)) % 7
    return 0


def _session_matches(
    sheet: dict[str, Any],
    local: datetime,
    span: int,
    shift_hr: float,
    lat: float | None,
    lon: float | None,
) -> bool | None:
    """(day, startTime) to (dayTil, endTime) as one interval; an endTime of
    00:00 means the very start of dayTil (the end-of-day rule)."""
    minute = local.hour * 60 + local.minute
    first = WEEKDAY_NAMES.index(sheet["day"].upper())
    unknown = False
    for back in range(span + 2):
        day = local.date() - timedelta(days=back)
        if day.weekday() != first:
            continue
        date_ok = _date_ok(sheet, day)
        if date_ok is False:
            continue
        start = _edge(sheet, "start", day, shift_hr, lat, lon)
        end = _edge(sheet, "end", day + timedelta(days=span), shift_hr, lat, lon)
        if _minutes(sheet.get("end_time")) == 0:
            end = 0
        if start is None or end is None or date_ok is None:
            unknown = True
            continue
        if start <= minute + 1440 * back < end + 1440 * span:
            return True
    return None if unknown else False


def _sheet_matches(
    sheet: dict[str, Any],
    local: datetime,
    shift_hr: float,
    lat: float | None,
    lon: float | None,
) -> bool | None:
    """Whether the local moment is inside this sheet. A daily window whose end
    is before its start runs into the next day; day..dayTil is a session."""
    span = _session_span_days(sheet)
    if span:
        return _session_matches(sheet, local, span, shift_hr, lat, lon)
    minute = local.hour * 60 + local.minute
    unknown = False
    for offset_days in (0, 1):  # today's window, then yesterday's overnight tail
        day = local.date() - timedelta(days=offset_days)
        days_ok = [_date_ok(sheet, day), _weekday_ok(sheet, day)]
        if False in days_ok:
            continue
        start = _edge(sheet, "start", day, shift_hr, lat, lon)
        end = _edge(sheet, "end", day, shift_hr, lat, lon)
        if start is None or end is None or None in days_ok:
            unknown = True
            continue
        if end <= start:
            end += 1440  # overnight span
        position = minute + 1440 * offset_days
        if start <= position < end:
            return True
    return None if unknown else False


def _evaluate(sheets, at, shift_hr, lat, lon) -> bool | None:
    local = (at + timedelta(hours=shift_hr)).replace(tzinfo=None)
    included, excluded = [], []
    for sheet in sheets:
        result = _sheet_matches(sheet, local, shift_hr, lat, lon)
        is_excluded = (sheet.get("excluded") or "").upper() == "YES"
        (excluded if is_excluded else included).append(result)
    if True in excluded:
        return False
    if True in included:
        return None if None in excluded else True
    return None if None in included or None in excluded else False


def sheet_text(sheet: dict[str, Any]) -> str:
    """'MON-FRI 07:00-22:00' / 'ANY SR-SS', plus a date range when not the
    whole year."""
    days = (sheet.get("day") or "ANY").upper()
    if sheet.get("day_til"):
        days += f"-{sheet['day_til'].upper()}"
    start = sheet.get("start_time") or sheet.get("start_event") or "?"
    end = sheet.get("end_time") or sheet.get("end_event") or "?"
    text = f"{days} {start}-{end}"
    dates = (_md(sheet, "start"), _md(sheet, "end"))
    if all(dates) and dates != ("01-01", "12-31"):
        text += f" ({dates[0]} to {dates[1]})"
    if (sheet.get("excluded") or "").upper() == "YES":
        text = "except " + text
    return text


def published_times_status(
    sheets: list[dict[str, Any]] | None,
    at: datetime,
    *,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Whether ``at`` (aware UTC) is inside an SUA's published times of use."""
    if isinstance(sheets, str):
        sheets = json.loads(sheets)
    sheets = [s for s in (sheets or []) if isinstance(s, dict)]
    if not sheets:
        return {
            "in_published_times": None,
            "evaluated_local_time": None,
            "published_times": [],
            "times_are": None,
            "note": (
                "No times of use in the FAA SAA file (they may be by NOTAM, "
                "continuous, or not published); rely on NOTAM activation."
            ),
        }
    offsets = {s.get("time_offset_hr") for s in sheets}
    offset = float(next(iter(offsets)) or 0.0) if len(offsets) == 1 else None
    notes: list[str] = []
    if offset is None:
        result, local_label = None, None
        notes.append("Timesheets use different time offsets; not evaluated.")
    else:
        standard = (at + timedelta(hours=offset)).replace(tzinfo=None)
        dst = (
            offset != 0
            and offset not in NO_DST_OFFSETS
            and us_dst_in_effect(standard)
        )
        if dst and offset in AMBIGUOUS_DST_OFFSETS:
            readings = {
                _evaluate(sheets, at, offset, lat, lon),
                _evaluate(sheets, at, offset + 1, lat, lon),
            }
            result = next(iter(readings)) if len(readings) == 1 else None
            if len(readings) > 1:
                notes.append(
                    "UTC-7 airspace: the answer depends on whether local time "
                    "observes daylight saving (Arizona does not)."
                )
            shift, zone = offset, f"UTC{offset:+g}, or UTC{offset + 1:+g} with DST"
        else:
            shift = offset + (1 if dst else 0)
            zone = f"UTC{shift:+g}{', daylight saving' if dst else ''}"
            result = _evaluate(sheets, at, shift, lat, lon)
        local = at + timedelta(hours=shift)
        local_label = f"{local:%a %Y-%m-%d %H:%M} local ({zone})"
    if any((s.get("intermittent") or "").upper() == "YES" for s in sheets):
        notes.append("Published as intermittent: in use only part of these times.")
    issues = sorted(
        {i for s in sheets for i in (s.get("issues") or []) if i != "local_time_offset"}
    )
    if issues:
        notes.append("Timesheet anomalies: " + ", ".join(issues) + ".")
    if result is None and not notes:
        notes.append(
            "Could not be evaluated (unknown day value, missing sunrise/sunset "
            "location, or an unparsed time)."
        )
    return {
        "in_published_times": result,
        "evaluated_local_time": local_label,
        "published_times": [sheet_text(s) for s in sheets],
        "times_are": f"local, UTC{offset:+g} standard" if offset else "UTC",
        "note": " ".join(notes) or None,
    }
