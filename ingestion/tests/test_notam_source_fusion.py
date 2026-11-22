"""Tests for deterministic digital/text NOTAM source fusion."""

from nfs_ingest.services.notam_source_fusion import fuse_notam_geometry
from nfs_ingest.services.notam_text_parser import AirspaceTextParser

POLYGON_TEXT = (
    "AIRSPACE BOUNDED BY 40.0N 75.0W 40.0N 74.0W "
    "41.0N 74.0W 41.0N 75.0W 5000FT MSL-10000FT MSL"
)
DIGITAL_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-77, 42], [-76, 42], [-76, 43], [-77, 43], [-77, 42]]],
}


def parse(text: str) -> dict:
    return AirspaceTextParser.parse(
        text,
        reference_lookup={},
        airport_lookup={},
        include_aircraft_restrictions=False,
    )


def test_digital_area_wins_over_text_area() -> None:
    fused = fuse_notam_geometry(
        digital_geometry=DIGITAL_POLYGON,
        digital_vertical_limits={},
        text_parse=parse(POLYGON_TEXT),
    )

    assert fused.geometry_geojson == DIGITAL_POLYGON
    assert fused.geometry_source == "AIXM"
    assert fused.parse_confidence == 1


def test_text_area_replaces_digital_representative_point() -> None:
    fused = fuse_notam_geometry(
        digital_geometry={"type": "Point", "coordinates": [-75, 40]},
        digital_geometry_source="GML_ARP_FALLBACK",
        digital_vertical_limits={},
        text_parse=parse(POLYGON_TEXT),
    )

    assert fused.geometry_geojson == parse(POLYGON_TEXT)["geometry"]
    assert fused.geometry_source == "TEXT_POLYGON"


def test_text_fills_missing_digital_altitude_bounds() -> None:
    fused = fuse_notam_geometry(
        digital_geometry=DIGITAL_POLYGON,
        digital_vertical_limits={},
        text_parse=parse(POLYGON_TEXT),
    )

    assert fused.lower_altitude_ft == 5000
    assert fused.upper_altitude_ft == 10000
    assert fused.lower_altitude_reference == "MSL"


def test_conflicting_text_does_not_replace_digital_bounds() -> None:
    fused = fuse_notam_geometry(
        digital_geometry=DIGITAL_POLYGON,
        digital_vertical_limits={
            "lower_ft": 6000,
            "upper_ft": 9000,
            "lower_reference": "MSL",
            "upper_reference": "MSL",
        },
        text_parse=parse(POLYGON_TEXT),
    )

    assert fused.lower_altitude_ft == 6000
    assert fused.upper_altitude_ft == 9000
    assert len(fused.warnings) == 2


def test_text_circle_preserves_source_radius_and_center() -> None:
    fused = fuse_notam_geometry(
        digital_geometry=None,
        text_parse=parse("AIRSPACE WI 5NM RADIUS OF 40.0N 75.0W SFC-FL 180"),
    )

    assert fused.geometry_source == "TEXT_CIRCLE"
    assert fused.horizontal_radius_nm == 5
    assert fused.circle_center_longitude == -75
    assert fused.circle_center_latitude == 40
    assert fused.upper_altitude_reference == "STD"


def test_ambiguous_text_does_not_emit_hazard_geometry() -> None:
    fused = fuse_notam_geometry(
        digital_geometry=None,
        text_parse=parse(POLYGON_TEXT + " THEN CLOCKWISE ARC"),
    )

    assert fused.usable is False
    assert fused.geometry_source == "UNRESOLVED"
    assert fused.warnings

