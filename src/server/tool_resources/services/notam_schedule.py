"""Parse and evaluate NOTAM schedules (ICAO item D / NMS ``schedule``).

A NOTAM's effective window says when it is in force; its schedule says at
which times inside that window it actually applies, e.g. ``DLY 1230-2330``,
``MON-FRI 1300-2100``, ``SAT SUN``, ``SR-SS``, ``02 04-06 09-13 0000/0834``
or ``NOV 21 1600-2359, 22 0000-1559``. Times are UTC; a range whose end is
not after its start runs past midnight. ``SR``/``SS`` are computed for the
NOTAM's location when one is supplied.

Evaluation is three-valued: True (applies), False (outside the schedule) or
None (schedule present but not understood, or sunrise/sunset needed without
a location). Callers should treat None as "may apply".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from server.tool_resources.constants import (
    ALL_DAY_TIMES,
    MONTH_INDEX,
    NOTAM_REWRITES,
    NOTAM_TOKEN_RE,
    WEEKDAY_INDEX,
)


@dataclass
class ScheduleGroup:
    """Day selectors plus the UTC time ranges that apply on those days."""

    dates: set[date] = field(default_factory=set)
    weekdays: set[int] = field(default_factory=set)
    daily: bool = False
    times: list[tuple[str, str]] = field(default_factory=list)

    def has_selector(self) -> bool:
        return self.daily or bool(self.dates) or bool(self.weekdays)

    def selects(self, day: date) -> bool:
        if self.daily or not (self.dates or self.weekdays):
            return True
        return day in self.dates or day.weekday() in self.weekdays


@dataclass
class NotamSchedule:
    text: str
    groups: list[ScheduleGroup] = field(default_factory=list)
    excluded_dates: set[date] = field(default_factory=set)
    excluded_weekdays: set[int] = field(default_factory=set)
    # Explicit start/end pairs (YYMMDDHHMM) listed in the schedule.
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)

    @property
    def parsed(self) -> bool:
        return bool(self.groups or self.intervals) and not self.unparsed


def schedule_text(raw: str | None) -> str | None:
    """The human item-D text from an NMS ``schedule`` value.

    NMS sends ``"<structured>~<item D>"`` (e.g. ``Daily:1230-2330~DLY
    1230-2330``) or the item D text alone.
    """
    if not raw or not str(raw).strip():
        return None
    text = str(raw).split("~", 1)[-1]
    return " ".join(text.replace("\\r", " ").replace("\\n", " ").split()) or None


def parse_schedule(raw: str | None, effective_start: datetime | None) -> NotamSchedule:
    """Parse item D. Day numbers without a month use the effective-start
    month and roll into the next month when they decrease. Calendar selectors
    without an effective start remain unresolved rather than using today's
    date as an implicit anchor."""
    text = (schedule_text(raw) or "").upper()
    schedule = NotamSchedule(text=text)
    if not text:
        return schedule
    for pattern, replacement in NOTAM_REWRITES:
        text = pattern.sub(replacement, text)
    stamps: list[datetime] = []
    base = effective_start.astimezone(UTC).date() if effective_start else None
    month: int | None = None
    year = base.year if base else None
    last_day: int | None = None
    current = ScheduleGroup()
    excluding = False

    def flush(force: bool = False) -> None:
        nonlocal current
        if current.times or (force and current.has_selector()):
            if not current.times:
                current.times.append(ALL_DAY_TIMES)
            schedule.groups.append(current)
            current = ScheduleGroup()

    def resolve(day_number: int) -> date | None:
        nonlocal month, year, last_day
        if base is None or year is None:
            return None
        if month is None:
            month, year = base.month, base.year
            if day_number < base.day:  # day already passed: next month
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
        elif last_day is not None and day_number < last_day:
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        last_day = day_number
        try:
            return date(year, month, day_number)
        except ValueError:
            return None

    def add_dates(first: int, last: int) -> None:
        nonlocal last_day
        start = resolve(first)
        if start is None:
            schedule.unparsed.append(f"{first:02d}")
            return
        if last < first:  # e.g. 30-02 spans a month end
            end = resolve(last)
        else:
            try:
                end = date(start.year, start.month, last)
            except ValueError:
                end = None
            if end is not None:
                last_day = last
        if end is None:
            schedule.unparsed.append(f"{first:02d}-{last:02d}")
            return
        days: set[date] = set()
        cursor = start
        while end is not None and cursor <= end and len(days) < 62:
            days.add(cursor)
            cursor += timedelta(days=1)
        (schedule.excluded_dates if excluding else current.dates).update(days)

    for match in NOTAM_TOKEN_RE.finditer(text):
        kind = match.lastgroup
        value = re.sub(r"\s+", "", match.group())
        if kind in ("ws", "skip"):
            continue
        if kind == "sep":
            flush()
            continue
        if kind == "exc":
            flush(force=True)
            excluding = True
            continue
        if kind == "stamp":
            try:
                stamp = datetime.strptime(value, "%y%m%d%H%M")
                stamps.append(stamp.replace(tzinfo=UTC))
            except ValueError:
                schedule.unparsed.append(value)
            continue
        if kind == "time":
            if excluding:
                schedule.unparsed.append(value)
                continue
            start, end = re.split(r"[-/]", value, maxsplit=1)
            current.times.append((start, end))
            continue
        if kind == "h24":
            if not excluding:
                current.daily = True
                current.times.append(ALL_DAY_TIMES)
            continue
        if kind == "other":
            schedule.unparsed.append(value)
            continue
        # A selector after time ranges starts a new group.
        if current.times and not excluding:
            flush()
        if kind == "month":
            if base is None or year is None:
                schedule.unparsed.append(value)
                continue
            new_month = MONTH_INDEX[value]
            if month is not None and new_month < month:
                year += 1
            elif month is None and new_month < base.month:
                year = base.year + 1
            month, last_day = new_month, None
        elif kind == "day":
            add_dates(int(value), int(value))
        elif kind == "dayrange":
            first, last = (int(part) for part in value.split("-"))
            add_dates(first, last)
        elif kind == "xrange":  # 30-NOV 02: runs into the named month
            first_text, rest = value.split("-", 1)
            start = resolve(int(first_text))
            new_month, last_text = MONTH_INDEX[rest[:3]], rest[3:]
            reference = start or base
            if reference is None:
                schedule.unparsed.append(value)
                continue
            end_year = reference.year + (new_month < reference.month)
            try:
                end = date(end_year, new_month, int(last_text))
            except ValueError:
                end = None
            if start is None or end is None or end < start:
                schedule.unparsed.append(value)
            else:
                month, year, last_day = new_month, end_year, end.day
                span = (end - start).days + 1
                days = {start + timedelta(days=k) for k in range(span)}
                (schedule.excluded_dates if excluding else current.dates).update(days)
        elif kind == "wd":
            target = schedule.excluded_weekdays if excluding else current.weekdays
            target.add(WEEKDAY_INDEX[value])
        elif kind == "wdrange":
            first, last = (WEEKDAY_INDEX[part] for part in value.split("-"))
            span = {(first + offset) % 7 for offset in range((last - first) % 7 + 1)}
            (schedule.excluded_weekdays if excluding else current.weekdays).update(
                span
            )
        elif kind == "daily" and not excluding:
            current.daily = True
    if not excluding or current.times:
        flush(force=True)
    if stamps:
        if len(stamps) % 2:
            schedule.unparsed.append("odd number of date-time stamps")
        for start, end in zip(stamps[::2], stamps[1::2], strict=False):
            if end <= start:
                schedule.unparsed.append(
                    f"non-increasing interval {start:%y%m%d%H%M}-{end:%y%m%d%H%M}"
                )
            else:
                schedule.intervals.append((start, end))
    return schedule


def _sun_times(day: date, lat: float, lon: float) -> tuple[datetime, datetime] | None:
    """Approximate sunrise/sunset (UTC) via the standard sunrise equation."""
    n = (day - date(2000, 1, 1)).days
    mean = n - lon / 360.0
    anomaly = math.radians((357.5291 + 0.98560028 * mean) % 360)
    center = (
        1.9148 * math.sin(anomaly)
        + 0.02 * math.sin(2 * anomaly)
        + 0.0003 * math.sin(3 * anomaly)
    )
    ecliptic = math.radians((math.degrees(anomaly) + center + 282.9372) % 360)
    transit = 2451545.0 + mean + 0.0053 * math.sin(anomaly) - 0.0069 * math.sin(
        2 * ecliptic
    )
    declination = math.asin(math.sin(ecliptic) * math.sin(math.radians(23.44)))
    phi = math.radians(lat)
    altitude = math.sin(math.radians(-0.833))
    cos_hour = (altitude - math.sin(phi) * math.sin(declination)) / (
        math.cos(phi) * math.cos(declination)
    )
    if abs(cos_hour) > 1:
        return None
    hour = math.degrees(math.acos(cos_hour)) / 360.0
    epoch = datetime(2000, 1, 1, 12, tzinfo=UTC)
    return (
        epoch + timedelta(days=transit - hour - 2451545.0),
        epoch + timedelta(days=transit + hour - 2451545.0),
    )


def _edge_time(
    edge: str,
    day: date,
    lat: float | None,
    lon: float | None,
    *,
    allow_2400: bool = False,
) -> datetime | None:
    match = re.fullmatch(r"(\d{4}|SR|SS)(?:(PLUS|MINUS)(\d{1,3}))?", edge)
    if not match:
        return None
    base, sign, minutes = match.groups()
    if base.isdigit():
        hours, mins = int(base[:2]), int(base[2:])
        if hours > 24 or mins > 59 or (
            hours == 24 and (mins or minutes or not allow_2400)
        ):
            return None
        moment = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
            hours=hours, minutes=mins
        )
    else:
        if lat is None or lon is None:
            return None
        sun = _sun_times(day, lat, lon)
        if sun is None:
            return None
        moment = sun[0] if base == "SR" else sun[1]
    if minutes:
        delta = timedelta(minutes=int(minutes))
        moment = moment + delta if sign == "PLUS" else moment - delta
    return moment


def is_scheduled(
    schedule: NotamSchedule,
    at: datetime,
    *,
    lat: float | None = None,
    lon: float | None = None,
) -> bool | None:
    """Whether ``at`` falls inside the schedule (None if undeterminable)."""
    if not schedule.text:
        return True
    if not schedule.parsed:
        return None
    at = at.astimezone(UTC)
    if any(start <= at < end for start, end in schedule.intervals):
        return True
    undetermined = False
    # Exclusions apply to the occurrence's starting day. Thus an allowed
    # Friday overnight occurrence may continue into an excluded Saturday.
    for offset in (1, 0):
        day = at.date() - timedelta(days=offset)
        if (
            day in schedule.excluded_dates
            or day.weekday() in schedule.excluded_weekdays
        ):
            continue
        for group in schedule.groups:
            if not group.selects(day):
                continue
            for start_edge, end_edge in group.times:
                start = _edge_time(start_edge, day, lat, lon)
                end = _edge_time(end_edge, day, lat, lon, allow_2400=True)
                if start is None or end is None:
                    undetermined = True
                    continue
                if end <= start:
                    end = _edge_time(
                        end_edge,
                        day + timedelta(days=1),
                        lat,
                        lon,
                        allow_2400=True,
                    )
                    if end is None:
                        undetermined = True
                        continue
                if start <= at < end:
                    return True
    return None if undetermined else False


def is_scheduled_during(
    schedule: NotamSchedule,
    window_start: datetime,
    window_end: datetime,
    *,
    lat: float | None = None,
    lon: float | None = None,
) -> bool | None:
    """Whether a schedule applies at any point in a UTC time window.

    ``None`` means the schedule could not be resolved conservatively. Date
    exclusions apply to the day on which an occurrence starts.
    """
    if not schedule.text:
        return True
    if not schedule.parsed:
        return None
    start = window_start.astimezone(UTC)
    end = window_end.astimezone(UTC)
    if end <= start:
        return is_scheduled(schedule, start, lat=lat, lon=lon)
    if any(
        item_start < end and item_end > start
        for item_start, item_end in schedule.intervals
    ):
        return True
    undetermined = False
    day = start.date() - timedelta(days=1)
    last_day = end.date()
    while day <= last_day:
        if (
            day not in schedule.excluded_dates
            and day.weekday() not in schedule.excluded_weekdays
        ):
            for group in schedule.groups:
                if not group.selects(day):
                    continue
                for start_edge, end_edge in group.times:
                    occurrence_start = _edge_time(start_edge, day, lat, lon)
                    occurrence_end = _edge_time(
                        end_edge, day, lat, lon, allow_2400=True
                    )
                    if occurrence_start is None or occurrence_end is None:
                        undetermined = True
                        continue
                    if occurrence_end <= occurrence_start:
                        occurrence_end = _edge_time(
                            end_edge,
                            day + timedelta(days=1),
                            lat,
                            lon,
                            allow_2400=True,
                        )
                        if occurrence_end is None:
                            undetermined = True
                            continue
                    if occurrence_start < end and occurrence_end > start:
                        return True
        day += timedelta(days=1)
    return None if undetermined else False
