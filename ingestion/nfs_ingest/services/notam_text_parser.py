"""Framework-independent parser for textual NOTAM airspace definitions."""

# ruff: noqa: E501

import re
from math import asin, atan2, cos, degrees, isfinite, radians, sin
from typing import Any

from nfs_ingest.services.notam_airport_reference import resolve_airport_reference
from nfs_ingest.services.notam_altitude import altitude_bound

TIER_1_PATTERN = re.compile(
    r"\b(\d{2})(\d{2})(\d{2}(?:\.\d+)?)\s*([NS])\s*(\d{3})(\d{2})(\d{2}(?:\.\d+)?)\s*([EW])\b"
)

TIER_2_PATTERN = re.compile(
    r"\b(\d{2})[°\s]+(\d{2})['\s]+(\d{2}(?:\.\d+)?)[\"\s]*([NS])[\s,]+(\d{3})[°\s]+(\d{2})['\s]+(\d{2}(?:\.\d+)?)[\"\s]*([EW])\b"
)

TIER_3_PATTERN = re.compile(
    r"\b(\d{2})(\d{2})\s*([NS])\s*(\d{3})(\d{2})\s*([EW])\b"
)

TIER_4_PATTERN = re.compile(
    r"\b(\d{1,2}\.\d+)\s*([NS])[\s,]+(\d{1,3}\.\d+)\s*([EW])\b"
)

DISTANCE_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"

RADIUS_PATTERN = re.compile(
    rf"\b(?:RADIUS\s*(?:OF\s*)?|(?:WI|WITHIN)\s+(?={DISTANCE_NUMBER}\s*(?:NM|KM)\s+(?:(?:RADIUS|RAD)\s+)?(?:OF|AROUND)\b))({DISTANCE_NUMBER})\s*(NM|KM)\b|(?<![\w.])({DISTANCE_NUMBER})\s*(NM|KM)\s*(?:RADIUS|RAD)\b",
    re.IGNORECASE,
)

AIRPORT_CLAUSE_END = re.compile(
    r"\s+(?=(?:SFC|GND|FL\s*\d|[+-]?\d+(?:\.\d+)?\s*(?:FT|M)\b|DLY\b|DAILY\b|BTN\b|MON\b|TUE\b|WED\b|THU\b|FRI\b|SAT\b|SUN\b|UNTIL\b|FROM\b))|[();\n]",
    re.IGNORECASE,
)

Q_LINE_PATTERN = re.compile(
    r"\bQ\)[^\r\n]*?(?=\s+[A-G]\)|[\r\n]|$)",
    re.IGNORECASE,
)

VERTICAL_PATTERN = re.compile(
    r"(?<![\w.])((?:SFC|GND|FL\s*\d+|[+-]?\d+(?:\.\d+)?\s*(?:FT|M))(?:\s*(?:AMSL|MSL|AGL))?)\s*(?:[-─/]|TO)\s*((?:FL\s*\d+|[+-]?\d+(?:\.\d+)?\s*(?:FT|M)|UNL)(?:\s*(?:AMSL|MSL|AGL))?)\b",
    re.IGNORECASE,
)

SUA_CODE_PATTERN = re.compile(
    r"\b([RWPAD]\s*[-]?\s*\d{2,4}[A-Z]?)(?![A-Z0-9/])\b",
    re.IGNORECASE,
)

SAA_NAME_PATTERN = re.compile(
    r"\b([A-Z0-9\-]+(?:\s+[A-Z0-9\-]+){0,4}\s+(?:MOA|NSA|ATCAA|TFR))\b",
    re.IGNORECASE,
)

LEADING_STOPWORDS = {
    "ALSO", "NEAR", "AND", "IN", "THE", "AT", "OVER", "WITHIN",
    "VIA", "TO", "FROM", "BOUNDED", "BY", "OF", "FOR", "AREA",
    "AIRSPACE", "RESTRICTED", "ACTIVE", "ALL", "SEE", "CONTACT",
}

# -----------------------------------------------------------------------------
# Aircraft Restriction Regex Patterns
# -----------------------------------------------------------------------------



def _dms_to_decimal(degrees_val, minutes_val, seconds_val, hemisphere):
    if not 0 <= float(minutes_val) < 60 or not 0 <= float(seconds_val) < 60:
        raise ValueError("Invalid coordinate minutes or seconds")
    decimal = (
        float(degrees_val)
        + float(minutes_val) / 60.0
        + float(seconds_val) / 3600.0
    )

    if hemisphere.upper() in {"S", "W"}:
        decimal *= -1

    return round(decimal, 7)


def _dm_to_decimal(degrees_val, minutes_val, hemisphere):
    if not 0 <= float(minutes_val) < 60:
        raise ValueError("Invalid coordinate minutes")
    decimal = float(degrees_val) + float(minutes_val) / 60.0

    if hemisphere.upper() in {"S", "W"}:
        decimal *= -1

    return round(decimal, 7)


def _clean_saa_name(raw_name: str) -> str:
    words = raw_name.strip().split()
    while words and words[0].upper() in LEADING_STOPWORDS:
        words.pop(0)
    return " ".join(words)


def _parse_and_normalize_altitude(val_str: str) -> dict[str, Any]:
    s = val_str.strip().upper()
    reference_match = re.search(r"\s*(AMSL|MSL|AGL)$", s)
    reference = reference_match.group(1) if reference_match else None
    if reference_match:
        s = s[:reference_match.start()].strip()
    unit = "FT"
    if s.endswith("FT"):
        s = s[:-2].strip()
    elif s.endswith("M"):
        s, unit = s[:-1].strip(), "M"
    value, reference = altitude_bound(s, unit, reference)
    return {"value": value, "unit": "FT", "reference": reference}


class AirspaceTextParser:
    @staticmethod
    def _coordinates(text):
        matches = []
        spans = []
        for tier, pattern in enumerate((TIER_1_PATTERN, TIER_2_PATTERN, TIER_3_PATTERN, TIER_4_PATTERN)):
            for match in pattern.finditer(text):
                if any(match.start() < end and match.end() > start for start, end in spans):
                    continue
                groups = match.groups()
                if tier < 2:
                    latitude = _dms_to_decimal(*groups[:4])
                    longitude = _dms_to_decimal(*groups[4:])
                elif tier == 2:
                    latitude = _dm_to_decimal(*groups[:3])
                    longitude = _dm_to_decimal(*groups[3:])
                else:
                    latitude = float(groups[0]) * (-1 if groups[1] == "S" else 1)
                    longitude = float(groups[2]) * (-1 if groups[3] == "W" else 1)
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError("Text coordinate outside WGS84 bounds")
                matches.append((match.start(), [longitude, latitude]))
                spans.append(match.span())
        coordinates = []
        for _, point in sorted(matches):
            if not coordinates or coordinates[-1] != point:
                coordinates.append(point)
        return coordinates

    @staticmethod
    def _build_circle_polygon(center, radius_val, unit="NM"):
        longitude = center[0]
        latitude = center[1]

        if unit.upper() == "KM":
            radius_nm = radius_val / 1.852
        else:
            radius_nm = radius_val

        earth_radius_nm = 3440.065
        angular_distance = radius_nm / earth_radius_nm

        coordinates = []

        for bearing in range(0, 361, 15):
            bearing_radians = radians(bearing)

            latitude_radians = radians(latitude)
            longitude_radians = radians(longitude)

            point_latitude = asin(
                sin(latitude_radians) * cos(angular_distance)
                + cos(latitude_radians)
                * sin(angular_distance)
                * cos(bearing_radians)
            )

            point_longitude = longitude_radians + atan2(
                sin(bearing_radians)
                * sin(angular_distance)
                * cos(latitude_radians),
                cos(angular_distance)
                - sin(latitude_radians) * sin(point_latitude),
            )

            coordinates.append(
                [
                    round(degrees(point_longitude), 6),
                    round(degrees(point_latitude), 6),
                ]
            )

        return {
            "type": "Polygon",
            "coordinates": [coordinates],
            "center": [round(longitude, 6), round(latitude, 6)],
        }

    @classmethod
    def _parse_aircraft_restrictions(cls, text_blob: str) -> dict[str, Any]:
        return {}

    @classmethod
    def reference_candidates(cls, text_blob: str) -> list[dict[str, Any]]:
        text_blob = text_blob.upper()
        candidates = []
        seen_normalized = set()

        for match in SUA_CODE_PATTERN.finditer(text_blob):
            raw = match.group(1).strip()
            norm = re.sub(r"[^A-Z0-9]", "", raw.upper())
            if norm and norm not in seen_normalized:
                seen_normalized.add(norm)
                candidates.append({
                    "raw": raw,
                    "normalized": norm,
                    "type": "SUA_CODE",
                })

        for match in SAA_NAME_PATTERN.finditer(text_blob):
            raw_full = match.group(1).strip()
            cleaned = _clean_saa_name(raw_full)
            if cleaned:
                norm = re.sub(r"[^A-Z0-9]", "", cleaned.upper())
                if norm and norm not in seen_normalized:
                    seen_normalized.add(norm)
                    candidates.append({
                        "raw": cleaned,
                        "normalized": norm,
                        "type": "SAA_NAME",
                    })

        return candidates

    @staticmethod
    def load_reference_lookup(candidates):
        return {}

    @classmethod
    def _parse_saa_references(cls, text_blob, reference_lookup=None):
        candidates = cls.reference_candidates(text_blob)
        if not candidates:
            return []
        if reference_lookup is None:
            reference_lookup = cls.load_reference_lookup(candidates)
        references = []
        for candidate in candidates:
            matches = reference_lookup.get(candidate["normalized"], [])
            match = matches[0] if len(matches) == 1 else {}
            references.append({
                **candidate,
                "matched_db": bool(match),
                "ambiguous": len(matches) > 1,
                "reference_id": match.get("id"),
                "designator": match.get("designator", candidate["raw"]),
                "name": match.get("name", candidate["raw"]),
                "geometry": match.get("geometry"),
            })
        return references

    @classmethod
    def parse(cls, raw_text, translation_text="", reference_lookup=None, airport_lookup=None, *, include_aircraft_restrictions=True):
        raw_text = Q_LINE_PATTERN.sub(" ", raw_text or "")
        translation_text = Q_LINE_PATTERN.sub(" ", translation_text or "")
        text_blob = f"{raw_text or ''} {translation_text or ''}".upper()
        coordinate_text = (raw_text or "").upper()
        patterns = (TIER_1_PATTERN, TIER_2_PATTERN, TIER_3_PATTERN, TIER_4_PATTERN)
        if not any(pattern.search(coordinate_text) for pattern in patterns) and (
            any(pattern.search((translation_text or "").upper()) for pattern in patterns)
            or not coordinate_text.strip()
            or (not RADIUS_PATTERN.search(coordinate_text) and RADIUS_PATTERN.search(translation_text or ""))
        ):
            coordinate_text = (translation_text or "").upper()
        geometry_warnings = []

        try:
            coordinates = cls._coordinates(coordinate_text)
        except ValueError as error:
            coordinates = []
            geometry_warnings.append(str(error))

        text_geometry = None
        if len(coordinates) >= 3:
            ring = list(coordinates)
            if ring[0] != ring[-1]:
                ring.append(ring[0])

            text_geometry = {
                "type": "Polygon",
                "coordinates": [ring],
            }
            if not re.search(r"\b(?:BOUNDED|BOUNDARY|POLYGON|AIRSPACE|AREA)\b", coordinate_text):
                geometry_warnings.append("Coordinate sequence lacks an explicit area context")
        elif len(coordinates) == 1:
            text_geometry = {
                "type": "Point",
                "coordinates": coordinates[0],
            }

        radius = None
        radius_match = RADIUS_PATTERN.search(coordinate_text)

        if radius_match:
            val = radius_match.group(1) or radius_match.group(3)
            unit = radius_match.group(2) or radius_match.group(4)
            if val and unit:
                radius = {
                    "value": float(val),
                    "unit": unit.upper(),
                }
        radius_values = {
            (float(match.group(1) or match.group(3)), (match.group(2) or match.group(4)).upper())
            for match in RADIUS_PATTERN.finditer(coordinate_text)
        }
        if len(radius_values) > 1:
            radius = None
            geometry_warnings.append("Multiple circle radii need component association")
        if radius and (not isfinite(radius["value"]) or not 0 < radius["value"] / (1.852 if radius["unit"] == "KM" else 1.0) < 10800):
            radius = None
            geometry_warnings.append("Circle radius must be finite, positive, and less than a hemisphere")
        airport_reference = None
        if not coordinates and not geometry_warnings:
            reference_text = None
            if radius and radius_match:
                tail = coordinate_text[radius_match.end():]
                center = re.match(r"\s*(?:(?:RADIUS|RAD)\s+)?(?:OF|AROUND|CENTERED\s+ON|CENTRED\s+ON)\s+(?:THE\s+)?(.+)", tail)
                if center:
                    reference_text = AIRPORT_CLAUSE_END.split(center.group(1), maxsplit=1)[0].strip(" .")
            else:
                center = re.search(r"\b(?:AT|OVER)\s+(?:THE\s+)?(.+)", coordinate_text)
                if center:
                    reference_text = AIRPORT_CLAUSE_END.split(center.group(1), maxsplit=1)[0].strip(" .")
            if reference_text:
                relative_reference = re.search(
                    rf"{DISTANCE_NUMBER}\s*(?:NM|KM)\b|\b(?:NEAR|VICINITY|BRG|BEARING)\b|\b(?:NORTH|SOUTH|EAST|WEST)\s+OF\b",
                    reference_text,
                )
                if relative_reference:
                    geometry_warnings.append("Airport-relative offset requires a resolved center; airport coordinates are not the NOTAM center")
                else:
                    airports = resolve_airport_reference(reference_text, airport_lookup)
                    if len(airports) == 1:
                        airport_reference = {**airports[0], "reference_text": reference_text}
                        coordinates = [list(airport_reference["coordinates"])]
                        text_geometry = {"type": "Point", "coordinates": coordinates[0]}
                    elif len(airports) > 1:
                        geometry_warnings.append("Ambiguous airport reference; no airport chosen")
                    elif radius:
                        geometry_warnings.append("Airport circle center could not be resolved")
        if radius and len(coordinates) != 1:
            geometry_warnings.append("Circle boundary requires exactly one unambiguous center")
        if re.search(r"\b(?:ARC|CLOCKWISE|COUNTERCLOCKWISE|EXCLUDING|EXCEPT)\b", coordinate_text):
            geometry_warnings.append("Complex text boundary needs explicit interpretation")

        circle_center = None
        if text_geometry and text_geometry.get("type") == "Point" and radius and radius["value"] > 0:
            circle_center = list(text_geometry["coordinates"])
            text_geometry = cls._build_circle_polygon(
                text_geometry["coordinates"],
                radius["value"],
                unit=radius["unit"],
            )

        intervals = []
        for vertical_match in VERTICAL_PATTERN.finditer(text_blob):
            lower_norm = _parse_and_normalize_altitude(vertical_match.group(1))
            upper_norm = _parse_and_normalize_altitude(vertical_match.group(2))
            interval = {
                "lower": lower_norm,
                "upper": upper_norm,
            }
            if interval not in intervals:
                intervals.append(interval)
        vertical_limits = intervals[0] if len(intervals) == 1 else {}
        vertical_warnings = []
        if len(intervals) > 1:
            vertical_warnings.append("Multiple distinct text altitude intervals need component association")
        if vertical_limits:
            lower, upper = vertical_limits["lower"], vertical_limits["upper"]
            if lower["reference"] == upper["reference"] and lower["value"] is not None and upper["value"] is not None and lower["value"] > upper["value"]:
                vertical_limits = {}
                vertical_warnings.append("Inverted text altitude interval")

        saa_references = cls._parse_saa_references(text_blob, reference_lookup)
        matched = {item["reference_id"]: item for item in saa_references if item.get("matched_db")}
        ambiguous_references = len(matched) > 1 or any(item.get("ambiguous") or not item.get("matched_db") for item in saa_references)
        saa_geometry = next(iter(matched.values()))["geometry"] if len(matched) == 1 and not ambiguous_references else None
        if ambiguous_references and (not text_geometry or text_geometry.get("type") != "Polygon"):
            geometry_warnings.append("Unresolved or multiple airspace references need component association")

        final_geometry = None
        geometry_source = "NONE"

        if text_geometry and text_geometry.get("type") == "Polygon" and circle_center is None:
            final_geometry = text_geometry
            geometry_source = "TEXT_POLYGON"
        elif text_geometry and text_geometry.get("type") == "Polygon" and circle_center is not None:
            final_geometry = text_geometry
            geometry_source = "TEXT_CIRCLE"
        elif saa_geometry:
            final_geometry = saa_geometry
            geometry_source = "SAA_AIRSPACE_LOOKUP"
        elif text_geometry and text_geometry.get("type") == "Point":
            final_geometry = text_geometry
            geometry_source = "AIRPORT_REFERENCE_LOOKUP" if airport_reference else "TEXT_POINT"
        if geometry_warnings:
            final_geometry = None
            circle_center = None
            geometry_source = "UNRESOLVED_TEXT"

        # Parse Aircraft Restrictions
        aircraft_restrictions = cls._parse_aircraft_restrictions(raw_text or translation_text or "") if include_aircraft_restrictions else {}

        return {
            "geometry": final_geometry,
            "geometry_source": geometry_source,
            "radius": radius,
            "vertical_limits": vertical_limits,
            "saa_references": saa_references,
            "airport_reference": airport_reference,
            "aircraft_restrictions": aircraft_restrictions,
            "point_count": len(coordinates),
            "source_text_length": len(text_blob),
            "geometry_warnings": geometry_warnings,
            "vertical_warnings": vertical_warnings,
            "circle_center": circle_center,
            "horizontal_radius_nm": radius["value"] / (1.852 if radius["unit"] == "KM" else 1.0) if circle_center is not None else 0.0,
        }

