"""Normalize FAA NMS GeoJSON features for Bronze and Silver storage."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nfs_ingest.config import settings
from nfs_ingest.services.notam_altitude import altitude_bound
from nfs_ingest.services.notam_source_fusion import (
    FusedNotamGeometry,
    fuse_notam_geometry,
)
from nfs_ingest.services.notam_text_parser import AirspaceTextParser

PARSER_VERSION = "nms-geojson-text-v2"

# NMS Q-line style coordinate, e.g. 3826N08212W or 382526N0820352W.
_QLINE_COORDINATE = re.compile(
    r"^(\d{2})(\d{2})(\d{2}(?:\.\d+)?)?([NS])(\d{3})(\d{2})(\d{2}(?:\.\d+)?)?([EW])$"
)
_EARTH_RADIUS_NM = 3440.065
# Obstacle height as published, e.g. "1139.8FT (316.9FT AGL)": MSL then AGL.
_OBSTACLE_HEIGHT = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*FT\s*\(\s*(\d+(?:\.\d+)?)\s*FT\s*AGL\s*\)"
)
OBSTACLE_GEOMETRY_SOURCE = "OBSTACLE_POINT_BUFFER"


@dataclass(frozen=True)
class PreparedNmsRecord:
    bronze: dict[str, Any]
    version: dict[str, Any]
    hazard: dict[str, Any] | None


# Non-timestamp NMS sentinels meaning "no fixed end" (e.g. effectiveEnd=PERM).
_OPEN_ENDED_VALUES = {"PERM", "UFN"}


def _timestamp(value: Any, fallback: datetime | None = None) -> datetime | None:
    if value in {None, ""}:
        return fallback
    if str(value).strip().upper() in _OPEN_ENDED_VALUES:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _translation_text(core: dict[str, Any]) -> str:
    values: list[str] = []
    for translation in core.get("notamTranslation") or []:
        if not isinstance(translation, dict):
            continue
        for key in ("formattedText", "icao_message", "domestic_message"):
            value = translation.get(key)
            if value and str(value).strip() not in values:
                values.append(str(value).strip())
    return "\n".join(values)


def _notam_id(notam: dict[str, Any]) -> str | None:
    number = str(notam.get("number") or "").strip().upper()
    series = str(notam.get("series") or "").strip().upper()
    if not number:
        return None
    return number if number[:1].isalpha() or not series else f"{series}{number}"


def _status(notam_type: Any) -> str:
    value = str(notam_type or "").strip().upper()
    return "CANCELLED" if value in {"C", "NOTAMC"} else "ACTIVE"


def _area_geometry(geometry: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(geometry, dict):
        return None, "NONE"
    geometry_type = geometry.get("type")
    if geometry_type in {"Polygon", "MultiPolygon"}:
        return geometry, "NMS_GEOJSON_AREA"
    if geometry_type == "GeometryCollection":
        areas = [
            item
            for item in geometry.get("geometries") or []
            if isinstance(item, dict)
            and item.get("type") in {"Polygon", "MultiPolygon"}
        ]
        polygons: list[Any] = []
        for item in areas:
            if item["type"] == "Polygon":
                polygons.append(item.get("coordinates") or [])
            else:
                polygons.extend(item.get("coordinates") or [])
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}, (
                "NMS_GEOJSON_AREA"
            )
        if polygons:
            return {"type": "MultiPolygon", "coordinates": polygons}, (
                "NMS_GEOJSON_AREA"
            )
        points = [
            item
            for item in geometry.get("geometries") or []
            if isinstance(item, dict) and item.get("type") == "Point"
        ]
        if len(points) == 1:
            return points[0], "NMS_GEOJSON_POINT"
        return None, "NONE"
    if geometry_type == "Point":
        return geometry, "NMS_GEOJSON_POINT"
    return geometry, "NMS_GEOJSON_UNSUPPORTED"


def _valid_lon_lat(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        longitude, latitude = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(longitude) and math.isfinite(latitude)):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


def _nms_point(geometry: Any) -> tuple[float, float] | None:
    """First Point in the NMS geometry (Point or GeometryCollection)."""
    if not isinstance(geometry, dict):
        return None
    if geometry.get("type") == "Point":
        return _valid_lon_lat(geometry.get("coordinates"))
    if geometry.get("type") == "GeometryCollection":
        for item in geometry.get("geometries") or []:
            if isinstance(item, dict) and item.get("type") == "Point":
                point = _valid_lon_lat(item.get("coordinates"))
                if point:
                    return point
    return None


def _qline_point(value: Any) -> tuple[float, float] | None:
    match = _QLINE_COORDINATE.match(str(value or "").strip().upper())
    if not match:
        return None
    lat_d, lat_m, lat_s, lat_h, lon_d, lon_m, lon_s, lon_h = match.groups()
    latitude = int(lat_d) + int(lat_m) / 60 + float(lat_s or 0) / 3600
    longitude = int(lon_d) + int(lon_m) / 60 + float(lon_s or 0) / 3600
    return _valid_lon_lat(
        (
            -longitude if lon_h == "W" else longitude,
            -latitude if lat_h == "S" else latitude,
        )
    )


def _text_point(text_parse: dict[str, Any]) -> tuple[float, float] | None:
    geometry = text_parse.get("geometry") or {}
    if isinstance(geometry, dict) and geometry.get("type") == "Point":
        point = _valid_lon_lat(geometry.get("coordinates"))
        if point:
            return point
    return _valid_lon_lat(text_parse.get("circle_center"))


def _hazard_center(fused: FusedNotamGeometry) -> tuple[float, float] | None:
    """Circle center, else bounding-box center of the usable hazard geometry."""
    if not fused.usable:
        return None
    center = _valid_lon_lat(
        (fused.circle_center_longitude, fused.circle_center_latitude)
    )
    if center:
        return center
    points: list[tuple[float, float]] = []

    def collect(value: Any) -> None:
        if not isinstance(value, (list, tuple)) or not value:
            return
        if all(isinstance(item, (int, float)) for item in value[:2]):
            point = _valid_lon_lat(value)
            if point:
                points.append(point)
            return
        for item in value:
            collect(item)

    collect((fused.geometry_geojson or {}).get("coordinates") or [])
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return _valid_lon_lat(
        (
            (min(longitudes) + max(longitudes)) / 2,
            (min(latitudes) + max(latitudes)) / 2,
        )
    )


def _obstacle_cylinder(
    raw_text: str,
    feature_geometry: Any,
    text_parse: dict[str, Any],
) -> tuple[FusedNotamGeometry, float] | None:
    """3D cylinder for a point-only OBST NOTAM with a published MSL height.

    The horizontal extent is a configurable modeling buffer
    (``settings.obstacle_buffer_nm``) around the reported obstacle point; it is
    stored as ``horizontal_buffer_nm`` and kept separate from any source
    radius. Vertical extent is SFC to the published MSL top. Obstacles with
    only an AGL height are left as point anchors because AGL and MSL are not
    interchangeable without terrain elevation.
    """
    text = (raw_text or "").upper()
    if not text.lstrip().startswith("OBST"):
        return None
    height = _OBSTACLE_HEIGHT.search(text)
    buffer_nm = float(settings.obstacle_buffer_nm or 0)
    if not height or buffer_nm <= 0:
        return None
    top_msl_ft, top_agl_ft = float(height.group(1)), float(height.group(2))
    if top_msl_ft <= 0:
        return None
    point = _nms_point(feature_geometry) or _text_point(text_parse)
    if not point:
        return None
    circle = AirspaceTextParser._build_circle_polygon(list(point), buffer_nm)
    circle.pop("center", None)
    fused = FusedNotamGeometry(
        geometry_geojson=circle,
        geometry_source=OBSTACLE_GEOMETRY_SOURCE,
        lower_altitude_ft=0.0,
        upper_altitude_ft=top_msl_ft,
        lower_altitude_reference="SFC",
        upper_altitude_reference="MSL",
        horizontal_radius_nm=0.0,
        circle_center_longitude=point[0],
        circle_center_latitude=point[1],
        parse_confidence=0.9,
        warnings=(
            f"Obstacle cylinder uses a {buffer_nm:g} NM modeling buffer around "
            f"the reported point; top is the published MSL height "
            f"({top_agl_ft:g} FT AGL).",
        ),
    )
    return fused, buffer_nm


def _distance_nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(h)))


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _anchor(
    feature_geometry: Any,
    notam: dict[str, Any],
    text_parse: dict[str, Any],
    airport_code_lookup: dict[str, list[dict[str, Any]]] | None,
    location: str | None,
    fused: FusedNotamGeometry,
) -> dict[str, Any]:
    """Best-available reference point for every NOTAM, including shapeless ones.

    This is a location anchor, never a hazard volume: point-only NOTAMs still
    do not emit hazard geometry. Priority: NMS point, center of the usable
    hazard shape, NOTAM text coordinate, NMS Q-line coordinate, then an
    airport reference lookup by location code.
    """
    nms_point = _nms_point(feature_geometry)
    qline_point = _qline_point(notam.get("coordinates"))
    candidates: list[tuple[str, tuple[float, float] | None]] = [
        ("NMS_POINT", nms_point),
        # A usable hazard shape describes the NOTAM better than a stray text
        # coordinate (e.g. a text circle center when digital area geometry won).
        ("HAZARD_CENTER", _hazard_center(fused)),
        ("TEXT_COORDINATE", _text_point(text_parse)),
        ("NMS_QLINE", qline_point),
    ]
    airports = (airport_code_lookup or {}).get(str(location or "").upper()) or []
    if len(airports) == 1:
        candidates.append(
            (
                "AIRPORT_REFERENCE",
                _valid_lon_lat(
                    (airports[0].get("longitude"), airports[0].get("latitude"))
                ),
            )
        )
    source, point = next(
        ((name, value) for name, value in candidates if value),
        ("NONE", None),
    )
    return {
        "anchor_longitude": point[0] if point else None,
        "anchor_latitude": point[1] if point else None,
        "anchor_source": source,
        "anchor_radius_nm": _positive_float(notam.get("radius")),
        "anchor_qline_offset_nm": (
            round(_distance_nm(nms_point, qline_point), 3)
            if nms_point and qline_point
            else None
        ),
    }


def _digital_vertical_limits(notam: dict[str, Any]) -> dict[str, Any]:
    limits: dict[str, Any] = {}
    for side, key, flight_level_key in (
        ("lower", "lowerLimit", "minimumFl"),
        ("upper", "upperLimit", "maximumFl"),
    ):
        raw = notam.get(key)
        if raw in {None, ""}:
            flight_level = str(notam.get(flight_level_key) or "").strip()
            if flight_level and flight_level != "999":
                raw = f"FL{flight_level}"
            elif side == "upper" and flight_level == "999":
                raw = "UNL"
        value, reference = altitude_bound(raw)
        if value is not None or reference != "UNKNOWN":
            limits[f"{side}_ft"] = value
            limits[f"{side}_reference"] = reference
    return limits


def _hazard_row(
    version_id: str,
    nms_id: str,
    notam_id: str | None,
    location: str | None,
    effective_start: datetime,
    effective_end: datetime | None,
    fused: FusedNotamGeometry,
    created_at: datetime,
    horizontal_buffer_nm: float = 0.0,
) -> dict[str, Any] | None:
    if not fused.usable:
        return None
    geometry_json = json.dumps(
        fused.geometry_geojson,
        sort_keys=True,
        separators=(",", ":"),
    )
    hazard_id = hashlib.sha256(
        f"{version_id}:{geometry_json}".encode("utf-8")
    ).hexdigest()
    return {
        "hazard_id": hazard_id,
        "notam_version_id": version_id,
        "nms_id": nms_id,
        "notam_id": notam_id,
        "location": location,
        "effective_start": effective_start.isoformat(),
        "effective_end": effective_end.isoformat() if effective_end else None,
        "geometry_geojson": geometry_json,
        "geometry_source": fused.geometry_source,
        "lower_altitude_ft": fused.lower_altitude_ft,
        "upper_altitude_ft": fused.upper_altitude_ft,
        "lower_altitude_reference": fused.lower_altitude_reference,
        "upper_altitude_reference": fused.upper_altitude_reference,
        "horizontal_radius_nm": fused.horizontal_radius_nm,
        "circle_center_longitude": fused.circle_center_longitude,
        "circle_center_latitude": fused.circle_center_latitude,
        "horizontal_buffer_nm": horizontal_buffer_nm,
        "h3_resolution": 7,
        "parse_confidence": fused.parse_confidence,
        "parse_warnings": list(fused.warnings),
        "parser_version": PARSER_VERSION,
        "source": "FAA_NMS",
        "created_at": created_at.isoformat(),
    }


def prepare_nms_feature(
    feature: dict[str, Any],
    *,
    fetched_at: datetime,
    request_mode: str,
    request_watermark: datetime | None,
    reference_lookup: dict[str, list[dict[str, Any]]] | None = None,
    airport_lookup: dict[str, list[dict[str, Any]]] | None = None,
    airport_code_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> PreparedNmsRecord:
    core = ((feature.get("properties") or {}).get("coreNOTAMData") or {})
    notam = core.get("notam") or {}
    nms_id = str(notam.get("id") or "").strip()
    if not nms_id:
        raise ValueError("NMS GeoJSON feature did not include coreNOTAMData.notam.id")

    raw_payload = json.dumps(feature, sort_keys=True, separators=(",", ":"))
    payload_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
    last_updated = _timestamp(notam.get("lastUpdated"), fetched_at) or fetched_at
    version_id = hashlib.sha256(
        f"{nms_id}:{last_updated.isoformat()}:{payload_sha256}".encode("utf-8")
    ).hexdigest()
    effective_start = (
        _timestamp(notam.get("effectiveStart"))
        or _timestamp(notam.get("issued"))
        or last_updated
    )
    effective_end = _timestamp(
        notam.get("effectiveEnd") or notam.get("cancelationDate")
    )
    raw_text = str(notam.get("text") or "").strip()
    translation_text = _translation_text(core)
    digital_geometry, digital_source = _area_geometry(feature.get("geometry"))
    text_parse = AirspaceTextParser.parse(
        raw_text,
        translation_text,
        reference_lookup=reference_lookup or {},
        airport_lookup=airport_lookup or {},
        include_aircraft_restrictions=False,
    )
    fused = fuse_notam_geometry(
        digital_geometry=digital_geometry,
        digital_geometry_source=digital_source,
        digital_vertical_limits=_digital_vertical_limits(notam),
        text_parse=text_parse,
    )
    notam_id = _notam_id(notam)
    location = str(
        notam.get("icaoLocation") or notam.get("location") or ""
    ) or None
    event_id = version_id
    bronze = {
        "event_id": event_id,
        "nms_id": nms_id,
        "fetched_at": fetched_at.isoformat(),
        "last_updated_at": last_updated.isoformat(),
        "request_mode": request_mode,
        "request_watermark": (
            request_watermark.isoformat() if request_watermark else None
        ),
        "payload_sha256": payload_sha256,
        "payload_json": raw_payload,
        "source": "FAA_NMS",
    }
    version = {
        "notam_version_id": version_id,
        "bronze_event_id": event_id,
        "nms_id": nms_id,
        "notam_id": notam_id,
        "notam_type": str(notam.get("type") or "") or None,
        "status": _status(notam.get("type")),
        "location": location,
        "replaces_notam_version_id": None,
        "effective_start": effective_start.isoformat(),
        "effective_end": effective_end.isoformat() if effective_end else None,
        "raw_text": raw_text or translation_text or None,
        "category": str(notam.get("selectionCode") or "") or None,
        "phase_of_flight": [],
        "valid_from": last_updated.isoformat(),
        "valid_to": None,
        "is_current": True,
        "parsed_at": fetched_at.isoformat(),
        "parser_version": PARSER_VERSION,
        "source": "FAA_NMS",
        **_anchor(
            feature.get("geometry"),
            notam,
            text_parse,
            airport_code_lookup,
            location,
            fused,
        ),
    }
    hazard_geometry, buffer_nm = fused, 0.0
    if not fused.usable:
        obstacle = _obstacle_cylinder(raw_text, feature.get("geometry"), text_parse)
        if obstacle:
            hazard_geometry, buffer_nm = obstacle
    hazard = _hazard_row(
        version_id,
        nms_id,
        notam_id,
        location,
        effective_start,
        effective_end,
        hazard_geometry,
        fetched_at,
        horizontal_buffer_nm=buffer_nm,
    )
    return PreparedNmsRecord(bronze=bronze, version=version, hazard=hazard)


def prepare_nms_payload(
    payload: dict[str, Any],
    *,
    fetched_at: datetime,
    request_mode: str,
    request_watermark: datetime | None,
    airport_code_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> list[PreparedNmsRecord]:
    features = (payload.get("data") or {}).get("geojson") or []
    if not isinstance(features, list):
        raise ValueError("NMS response data.geojson must be an array")
    latest_by_nms_id: dict[str, PreparedNmsRecord] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        prepared = prepare_nms_feature(
            feature,
            fetched_at=fetched_at,
            request_mode=request_mode,
            request_watermark=request_watermark,
            airport_code_lookup=airport_code_lookup,
        )
        nms_id = prepared.version["nms_id"]
        previous = latest_by_nms_id.get(nms_id)
        if previous is None or prepared.version["valid_from"] >= previous.version[
            "valid_from"
        ]:
            latest_by_nms_id[nms_id] = prepared
    return list(latest_by_nms_id.values())

