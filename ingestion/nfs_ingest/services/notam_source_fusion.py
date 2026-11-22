"""Deterministic fusion of digital and text-derived NOTAM airspace data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nfs_ingest.services.notam_altitude import merge_vertical_limits

_AREA_TYPES = {"Polygon", "MultiPolygon"}


@dataclass(frozen=True)
class FusedNotamGeometry:
    geometry_geojson: dict[str, Any] | None
    geometry_source: str
    lower_altitude_ft: float | None
    upper_altitude_ft: float | None
    lower_altitude_reference: str
    upper_altitude_reference: str
    horizontal_radius_nm: float
    circle_center_longitude: float | None
    circle_center_latitude: float | None
    parse_confidence: float
    warnings: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.geometry_geojson is not None


def _geometry_type(geometry: Any) -> str:
    if not isinstance(geometry, dict):
        return ""
    return str(geometry.get("type") or "")


def fuse_notam_geometry(
    *,
    digital_geometry: dict[str, Any] | None,
    digital_geometry_source: str = "AIXM",
    digital_vertical_limits: dict[str, Any] | None = None,
    text_parse: dict[str, Any],
) -> FusedNotamGeometry:
    """Fuse trusted digital geometry with conservative text-derived fallback."""
    warnings = list(text_parse.get("geometry_warnings") or [])
    warnings.extend(text_parse.get("vertical_warnings") or [])

    digital_type = _geometry_type(digital_geometry)
    text_geometry = text_parse.get("geometry")
    text_type = _geometry_type(text_geometry)
    text_is_area = text_type in _AREA_TYPES and not text_parse.get(
        "geometry_warnings"
    )

    if digital_type in _AREA_TYPES:
        geometry = digital_geometry
        geometry_source = digital_geometry_source
        confidence = 1.0
    elif text_is_area:
        geometry = text_geometry
        geometry_source = str(text_parse.get("geometry_source") or "TEXT")
        confidence = 0.8
    else:
        geometry = None
        geometry_source = "UNRESOLVED"
        confidence = 0.0
        if digital_geometry is not None and digital_type not in {"", "Point"}:
            warnings.append(f"Unsupported digital geometry type: {digital_type}")
        if text_type == "Point" and not text_parse.get("horizontal_radius_nm"):
            warnings.append("Text point is not a verified airspace footprint")

    limits, limit_warnings = merge_vertical_limits(
        digital_vertical_limits or {},
        text_parse.get("vertical_limits") or {},
    )
    warnings.extend(limit_warnings)

    circle_center = text_parse.get("circle_center")
    use_text_circle = geometry_source == "TEXT_CIRCLE" and circle_center
    return FusedNotamGeometry(
        geometry_geojson=geometry,
        geometry_source=geometry_source,
        lower_altitude_ft=limits["lower_ft"],
        upper_altitude_ft=limits["upper_ft"],
        lower_altitude_reference=str(limits["lower_reference"]),
        upper_altitude_reference=str(limits["upper_reference"]),
        horizontal_radius_nm=(
            float(text_parse.get("horizontal_radius_nm") or 0)
            if use_text_circle
            else 0.0
        ),
        circle_center_longitude=(float(circle_center[0]) if use_text_circle else None),
        circle_center_latitude=(float(circle_center[1]) if use_text_circle else None),
        parse_confidence=confidence,
        warnings=tuple(dict.fromkeys(warnings)),
    )

