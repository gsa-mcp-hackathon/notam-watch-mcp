"""FAA Special Use Airspace (SAA/SUA AIXM 5.0) reference shapes.

Source: the NASR 28-day subscription, file
``Additional_Data/AIXM/SAA-AIXM_5_Schema/SaaSubscriberFile.zip`` (which wraps
``Saa_Sub_File.zip``: one SaaMessage XML per airspace: MOA, restricted,
warning, alert, prohibited areas and NSAs). Only that ~5 MB member is
fetched, using HTTP range requests against the ~250 MB subscription zip.

NOTAMs refer to these airspaces by name (``AIRSPACE R5107K ACT SFC-FL300``,
``LIVE OAK MOA ACT``, a TFR "WI THE AREA DESCRIBED AS R3807") without
coordinates; this table lets the gold layer draw them.

Geometry: each airspace has BASE / UNION / SUBTR components whose exterior is
either a LinearRing of positions or a Ring of LineStringSegment,
ArcByCenterPoint (angles counter-clockwise from east, as in GML) and
CircleByCenterPoint segments. Arcs are densified on the sphere; components
are combined in PostGIS at publish time.
"""

from __future__ import annotations

import io
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import requests

from nfs_ingest.config import settings
from server.tool_resources.constants import PROHIBITED_DAY_VALUES
from server.tool_resources.services.sua_schedule import month_day

SAA_MEMBER = "Additional_Data/AIXM/SAA-AIXM_5_Schema/SaaSubscriberFile.zip"
EARTH_RADIUS_NM = 3440.065
ARC_STEP_DEG = 3.0


def subscription_url(effective: date) -> str:
    return (
        f"{settings.nasr_base_url.rstrip('/')}/"
        f"28DaySubscription_Effective_{effective:%Y-%m-%d}.zip"
    )


class _HttpRangeFile(io.RawIOBase):
    """Seekable read-only view of a remote file via HTTP Range requests."""

    def __init__(self, url: str, timeout: int = 300) -> None:
        self.url, self.timeout, self.position = url, timeout, 0
        self.session = requests.Session()
        response = self.session.get(url, headers={"Range": "bytes=0-0"}, timeout=60)
        response.raise_for_status()
        content_range = response.headers.get("content-range", "")
        if "/" not in content_range:
            raise RuntimeError(f"Server does not support range requests: {url}")
        self.size = int(content_range.rsplit("/", 1)[1])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.position, 2: self.size}[whence]
        self.position = base + offset
        return self.position

    def readinto(self, buffer: Any) -> int:
        if self.position >= self.size or not len(buffer):
            return 0
        end = min(self.position + len(buffer), self.size) - 1
        response = self.session.get(
            self.url,
            headers={"Range": f"bytes={self.position}-{end}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.content
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)


def download_saa_messages(effective: date) -> Iterator[tuple[str, bytes]]:
    """Yield (file name, XML bytes) for every SaaMessage in a NASR cycle."""
    source = _HttpRangeFile(subscription_url(effective))
    remote = zipfile.ZipFile(io.BufferedReader(source, buffer_size=1 << 20))
    outer = zipfile.ZipFile(io.BytesIO(remote.read(SAA_MEMBER)))
    inner_name = next(n for n in outer.namelist() if n.lower().endswith(".zip"))
    inner = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))
    for name in sorted(inner.namelist()):
        if name.lower().endswith(".xml"):
            yield name, inner.read(name)


# ------------------------------------------------------------------ parsing


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in element.iter() if _local(node.tag) == name]


def _first_text(element: ET.Element, name: str) -> str | None:
    for node in element.iter():
        if _local(node.tag) == name and node.text and node.text.strip():
            return node.text.strip()
    return None


def _position(element: ET.Element) -> tuple[float, float]:
    lon, lat = (float(v) for v in (element.text or "").split()[:2])
    return lon, lat


def destination(
    lon: float, lat: float, bearing_deg: float, distance_nm: float
) -> tuple[float, float]:
    """Great-circle destination point."""
    delta = distance_nm / EARTH_RADIUS_NM
    theta = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta)
        + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon2 = (math.degrees(lam2) + 540) % 360 - 180
    return round(lon2, 7), round(math.degrees(phi2), 7)


def _radius_nm(element: ET.Element) -> float:
    node = _descendants(element, "radius")[0]
    value = float(node.text or 0)
    unit = (node.get("uom") or "NM").upper()
    return {"NM": value, "KM": value / 1.852, "M": value / 1852, "FT": value / 6076.12,
            "MI": value * 0.868976}.get(unit, value)


def arc_points(
    center: tuple[float, float],
    radius_nm: float,
    start_angle: float,
    end_angle: float,
) -> list[tuple[float, float]]:
    """Points along an arc; angles are GML (counter-clockwise from east)."""
    steps = max(2, math.ceil(abs(end_angle - start_angle) / ARC_STEP_DEG))
    points = []
    for k in range(steps + 1):
        angle = start_angle + (end_angle - start_angle) * k / steps
        points.append(destination(center[0], center[1], 90.0 - angle, radius_nm))
    return points


def ring_coordinates(exterior: ET.Element) -> list[tuple[float, float]]:
    """Closed coordinate ring from a GML exterior (LinearRing or Ring)."""
    points: list[tuple[float, float]] = []
    linear = _descendants(exterior, "LinearRing")
    if linear:
        points = [_position(p) for p in _descendants(linear[0], "pos")]
    else:
        for segments in _descendants(exterior, "segments"):
            for segment in segments:
                kind = _local(segment.tag)
                if kind == "LineStringSegment":
                    points.extend(_position(p) for p in _descendants(segment, "pos"))
                elif kind in ("ArcByCenterPoint", "CircleByCenterPoint"):
                    center = _position(_descendants(segment, "pos")[0])
                    radius = _radius_nm(segment)
                    if kind == "CircleByCenterPoint":
                        points.extend(arc_points(center, radius, 0.0, 360.0))
                    else:
                        start = float(_first_text(segment, "startAngle") or 0)
                        end = float(_first_text(segment, "endAngle") or 0)
                        points.extend(arc_points(center, radius, start, end))
    deduped = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    if deduped and deduped[0] != deduped[-1]:
        deduped.append(deduped[0])
    return deduped


def _limit(volume: ET.Element, side: str) -> tuple[float | None, str]:
    """(feet, reference) using the gold conventions (AGL/MSL/STD/UNBOUNDED)."""
    nodes = _children(volume, f"{side}Limit")
    if not nodes:
        return None, "UNKNOWN"
    text = (nodes[0].text or "").strip().upper()
    unit = (nodes[0].get("uom") or "FT").upper()
    reference = (_first_text(volume, f"{side}LimitReference") or "").upper()
    if text in ("UNL", "UNLTD", "UNLIMITED"):
        return None, "UNBOUNDED"
    if text in ("GND", "SFC"):
        return 0.0, "AGL"
    try:
        value = float(text)
    except ValueError:
        return None, "UNKNOWN"
    if unit == "FL" or reference == "STD":
        return value * 100.0, "STD"
    return value, {"SFC": "AGL", "MSL": "MSL"}.get(reference, "UNKNOWN")


_WEEKDAYS = frozenset({"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"})


def _minutes(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::\d{2})?", (value or "").strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 24 or minutes > 59 or (hours == 24 and minutes):
        return None
    return hours * 60 + minutes


def timesheet_issues(sheet: dict[str, Any]) -> list[str]:
    """Digital NOTAM Timesheet rule violations for one parsed sheet.

    Codes:
    - time_reference_not_utc: timeReference is not UTC.
    - local_time_offset: a non-zero FAA timeOffset, so the clock times are
      local, not UTC.
    - daylight_saving_adjust: daylightSavingAdjust is present and not NO.
    - prohibited_day_value: day/dayTil is WORK_DAY, HOL, BUSY_FRI, etc.
    - weekday_with_date_range: a weekday is combined with a calendar range
      other than the whole year (the day must then be ANY).
    - invalid_date / invalid_time: a value that cannot be parsed.
    - overnight_span: the end time is before the start time in a one-day
      sheet (it should be split at 24:00, or coded with dayTil = next day).
    - midnight_end_as_0000: a one-day sheet ends at 00:00; read as 24:00.
    - missing_start / missing_end: neither a clock time nor a solar event.

    A sheet with dayTil on a later day is one continuous session from
    (day, startTime) to (dayTil, endTime), so '06:00-02:00 WED-THU' or an
    end of 00:00 on dayTil is the correct way to cross midnight, not an issue.
    """
    issues: list[str] = []
    reference = (sheet.get("time_reference") or "").upper()
    if reference and reference != "UTC":
        issues.append("time_reference_not_utc")
    if sheet.get("time_offset_hr"):
        issues.append("local_time_offset")
    dst = (sheet.get("daylight_saving_adjust") or "").upper()
    if dst and dst != "NO":
        issues.append("daylight_saving_adjust")

    day = (sheet.get("day") or "").upper()
    day_til = (sheet.get("day_til") or "").upper()
    if day in PROHIBITED_DAY_VALUES or day_til in PROHIBITED_DAY_VALUES:
        issues.append("prohibited_day_value")

    raw_dates = (sheet.get("start_date"), sheet.get("end_date"))
    dates = (sheet.get("start_md"), sheet.get("end_md"))
    if any(raw and not parsed for raw, parsed in zip(raw_dates, dates, strict=True)):
        issues.append("invalid_date")
    whole_year = dates == ("01-01", "12-31")
    if day in _WEEKDAYS and all(dates) and not whole_year:
        issues.append("weekday_with_date_range")

    start_raw, end_raw = sheet.get("start_time"), sheet.get("end_time")
    start, end = _minutes(start_raw), _minutes(end_raw)
    if (start_raw and start is None) or (end_raw and end is None):
        issues.append("invalid_time")
    one_day = not day_til or day_til == day
    if start is not None and end is not None and one_day:
        if end == 0 and start > 0:
            issues.append("midnight_end_as_0000")
        elif end < start:
            issues.append("overnight_span")
    if start_raw is None and not sheet.get("start_event"):
        issues.append("missing_start")
    if end_raw is None and not sheet.get("end_event"):
        issues.append("missing_end")
    return issues


def _timesheets(root: ET.Element) -> list[dict[str, Any]]:
    sheets = []
    for sheet in _descendants(root, "Timesheet"):
        offset = _first_text(sheet, "timeOffset")
        parsed = {
            "time_reference": _first_text(sheet, "timeReference"),
            "start_date": _first_text(sheet, "startDate"),
            "end_date": _first_text(sheet, "endDate"),
            "day": _first_text(sheet, "day"),
            "day_til": _first_text(sheet, "dayTil"),
            "start_time": _first_text(sheet, "startTime"),
            "end_time": _first_text(sheet, "endTime"),
            "start_event": _first_text(sheet, "startEvent"),
            "start_event_offset_min": _first_text(sheet, "startTimeRelativeEvent"),
            "start_event_interpretation": _first_text(
                sheet, "startEventInterpretation"
            ),
            "end_event": _first_text(sheet, "endEvent"),
            "end_event_offset_min": _first_text(sheet, "endTimeRelativeEvent"),
            "end_event_interpretation": _first_text(sheet, "endEventInterpretation"),
            "daylight_saving_adjust": _first_text(sheet, "daylightSavingAdjust"),
            "excluded": _first_text(sheet, "excluded"),
            "intermittent": _first_text(sheet, "intermittent"),
            "time_offset_hr": float(offset) if offset else None,
        }
        parsed["start_md"] = month_day(parsed["start_date"])
        parsed["end_md"] = month_day(parsed["end_date"])
        parsed["issues"] = timesheet_issues(parsed)
        sheets.append(parsed)
    return sheets


def normalize_designator(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def name_code(name: str | None) -> str:
    """'LIVE OAK MOA, FL' -> 'LIVE OAK MOA' (whitespace collapsed)."""
    return " ".join((name or "").split(",")[0].upper().split())


def parse_saa_message(xml_bytes: bytes, source_file: str = "") -> list[dict[str, Any]]:
    """Airspace records from one SaaMessage document."""
    root = ET.fromstring(xml_bytes)
    sua_type = _first_text(root, "suaType")
    schedule = _timesheets(root)
    records = []
    for airspace in _descendants(root, "Airspace"):
        designator = _first_text(airspace, "designator")
        name = _first_text(airspace, "name")
        if not (designator or name):
            continue
        components = []
        limits: dict[str, Any] = {}
        for component in _descendants(airspace, "AirspaceGeometryComponent"):
            operation = (_first_text(component, "operation") or "BASE").upper()
            sequence = int(_first_text(component, "operationSequence") or 0)
            volumes = _descendants(component, "AirspaceVolume")
            exteriors = _descendants(component, "exterior")
            if not volumes or not exteriors:
                continue
            ring = ring_coordinates(exteriors[0])
            if len(ring) < 4:
                continue
            holes = [ring_coordinates(e) for e in _descendants(component, "interior")]
            components.append(
                {
                    "operation": operation,
                    "sequence": sequence,
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [ring] + [h for h in holes if len(h) >= 4],
                    },
                }
            )
            if operation == "BASE" and not limits:
                lower_ft, lower_ref = _limit(volumes[0], "lower")
                upper_ft, upper_ref = _limit(volumes[0], "upper")
                limits = {
                    "lower_ft": lower_ft,
                    "lower_reference": lower_ref,
                    "upper_ft": upper_ft,
                    "upper_reference": upper_ref,
                }
        if not any(c["operation"] in ("BASE", "UNION") for c in components):
            continue
        records.append(
            {
                "designator": designator or name_code(name),
                "designator_normalized": normalize_designator(designator or name),
                "name": name,
                "name_code": name_code(name),
                "sua_type": sua_type,
                "administrative_area": _first_text(airspace, "administrativeArea"),
                **{
                    "lower_ft": None,
                    "lower_reference": "UNKNOWN",
                    "upper_ft": None,
                    "upper_reference": "UNKNOWN",
                    **limits,
                },
                "components_json": json.dumps(
                    sorted(components, key=lambda c: c["sequence"]),
                    separators=(",", ":"),
                ),
                "schedule_json": json.dumps(schedule, separators=(",", ":")),
                "source_file": source_file,
            }
        )
    return records


def dedupe_airspaces(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per designator. Some files embed copies of neighbouring
    airspaces (R-4812's file repeats R-4804A and R-4810); prefer the record
    from the airspace's own file."""
    chosen: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["designator_normalized"]
        own = (record.get("source_file") or "").upper().startswith(
            (record.get("name") or "").upper()[:20]
        )
        if key not in chosen or (own and not chosen[key].get("_own")):
            chosen[key] = {**record, "_own": own}
    return [
        {k: v for k, v in record.items() if k != "_own"} for record in chosen.values()
    ]


# ------------------------------------------------------------------ storage

_COLUMNS = (
    ("designator", "STRING"),
    ("designator_normalized", "STRING"),
    ("name", "STRING"),
    ("name_code", "STRING"),
    ("sua_type", "STRING"),
    ("administrative_area", "STRING"),
    ("lower_ft", "DOUBLE"),
    ("lower_reference", "STRING"),
    ("upper_ft", "DOUBLE"),
    ("upper_reference", "STRING"),
    ("components_json", "STRING"),
    ("schedule_json", "STRING"),
    ("source_file", "STRING"),
    ("nasr_effective_date", "DATE"),
    ("loaded_at", "TIMESTAMP"),
)


def replace_cycle(spark: Any, records: list[dict[str, Any]], effective: date) -> int:
    """Overwrite the SUA reference table with one NASR cycle."""
    loaded_at = datetime.now(UTC)
    rows = [
        tuple(
            {**r, "nasr_effective_date": effective, "loaded_at": loaded_at}[name]
            for name, _ in _COLUMNS
        )
        for r in records
    ]
    schema = ", ".join(f"{name} {kind}" for name, kind in _COLUMNS)
    table = ".".join(f"`{p}`" for p in settings.sua_airspaces_table.split("."))
    spark.createDataFrame(rows, schema=schema).write.mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(table)
    return len(rows)
