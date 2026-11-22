"""Tests for FAA NMS GeoJSON normalization and text fallback."""

import json
from datetime import UTC, datetime

from nfs_ingest.services.nms_parser import prepare_nms_feature, prepare_nms_payload

NOW = datetime(2026, 10, 27, 16, 0, tzinfo=UTC)


def feature(
    *,
    nms_id: str = "nms-1",
    updated: str = "2026-10-27T15:59:00Z",
    notam_type: str = "N",
    geometry: dict | None = None,
    text: str = "AIRSPACE WI 5NM RADIUS OF 40.0N 75.0W SFC-FL 180",
) -> dict:
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "coreNOTAMData": {
                "notam": {
                    "id": nms_id,
                    "series": "A",
                    "number": "1234",
                    "year": "26",
                    "type": notam_type,
                    "effectiveStart": "2026-10-27T15:00:00Z",
                    "effectiveEnd": "2026-10-27T18:00:00Z",
                    "lastUpdated": updated,
                    "icaoLocation": "KJFK",
                    "text": text,
                },
                "notamTranslation": [],
            }
        },
    }


def prepare(item: dict):
    return prepare_nms_feature(
        item,
        fetched_at=NOW,
        request_mode="incremental",
        request_watermark=NOW,
    )


def test_perm_effective_end_is_open_ended() -> None:
    item = feature()
    item["properties"]["coreNOTAMData"]["notam"]["effectiveEnd"] = "PERM"

    record = prepare(item)

    assert record.version["effective_end"] is None
    assert record.hazard is None or record.hazard["effective_end"] is None


def test_point_only_notam_gets_nms_point_anchor_without_hazard() -> None:
    item = feature(
        geometry={
            "type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [-82.0645, 38.4239]}],
        },
        text="OBST TOWER LGT U/S",
    )
    notam = item["properties"]["coreNOTAMData"]["notam"]
    notam["coordinates"] = "3826N08212W"
    notam["radius"] = "5"

    record = prepare(item)

    assert record.hazard is None
    assert record.version["anchor_source"] == "NMS_POINT"
    assert record.version["anchor_longitude"] == -82.0645
    assert record.version["anchor_latitude"] == 38.4239
    assert record.version["anchor_radius_nm"] == 5.0
    assert 5 < record.version["anchor_qline_offset_nm"] < 10


def test_qline_anchor_used_when_no_point_or_text_coordinate() -> None:
    item = feature(text="TWY A CLSD")
    item["properties"]["coreNOTAMData"]["notam"]["coordinates"] = "3826S08212E"

    record = prepare(item)

    assert record.version["anchor_source"] == "NMS_QLINE"
    assert record.version["anchor_latitude"] == -(38 + 26 / 60)
    assert record.version["anchor_longitude"] == 82 + 12 / 60
    assert record.version["anchor_qline_offset_nm"] is None


def test_airport_reference_anchor_is_last_resort() -> None:
    item = feature(text="AD AP CLSD")
    record = prepare_nms_feature(
        item,
        fetched_at=NOW,
        request_mode="incremental",
        request_watermark=NOW,
        airport_code_lookup={
            "KJFK": [{"longitude": -73.7789, "latitude": 40.6398}]
        },
    )

    assert record.version["anchor_source"] == "AIRPORT_REFERENCE"
    assert record.version["anchor_longitude"] == -73.7789


def test_area_notam_anchor_is_hazard_center() -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[-77, 42], [-76, 42], [-76, 43], [-77, 42]]],
    }

    record = prepare(feature(geometry=polygon))

    assert record.hazard is not None
    assert record.version["anchor_source"] == "HAZARD_CENTER"
    assert record.version["anchor_longitude"] == -76.5
    assert record.version["anchor_latitude"] == 42.5


def test_obstacle_with_msl_height_becomes_buffered_cylinder() -> None:
    item = feature(
        geometry={
            "type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [-82.0645, 38.4239]}],
        },
        text=(
            "OBST TOWER LGT (ASR 1221870) 382526.20N0820352.20W "
            "(6.5NM E 12V) 1139.8FT (316.9FT AGL) U/S"
        ),
    )

    record = prepare(item)

    assert record.hazard is not None
    assert record.hazard["geometry_source"] == "OBSTACLE_POINT_BUFFER"
    assert record.hazard["lower_altitude_ft"] == 0.0
    assert record.hazard["lower_altitude_reference"] == "SFC"
    assert record.hazard["upper_altitude_ft"] == 1139.8
    assert record.hazard["upper_altitude_reference"] == "MSL"
    assert record.hazard["horizontal_radius_nm"] == 0.0
    assert record.hazard["horizontal_buffer_nm"] == 0.5
    assert record.hazard["circle_center_longitude"] == -82.0645
    assert json.loads(record.hazard["geometry_geojson"])["type"] == "Polygon"
    assert record.version["anchor_source"] == "NMS_POINT"


def test_obstacle_with_only_agl_height_stays_point_anchor() -> None:
    item = feature(
        geometry={"type": "Point", "coordinates": [-87.47, 41.66]},
        text="OBST TOWER LGT (ASR UNKNOWN) (3NM NW GYY) UNKNOWN (226FT AGL) U/S",
    )

    record = prepare(item)

    assert record.hazard is None
    assert record.version["anchor_source"] == "NMS_POINT"


def test_missing_location_data_yields_none_anchor() -> None:
    record = prepare(feature(text="AD AP CLSD"))

    assert record.version["anchor_source"] == "NONE"
    assert record.version["anchor_longitude"] is None


def test_digital_polygon_wins_over_text_geometry() -> None:
    polygon = {
        "type": "Polygon",
        "coordinates": [[[-77, 42], [-76, 42], [-76, 43], [-77, 42]]],
    }

    record = prepare(feature(geometry=polygon))

    assert record.hazard is not None
    assert record.hazard["geometry_source"] == "NMS_GEOJSON_AREA"
    assert record.hazard["parse_confidence"] == 1


def test_point_uses_text_circle_fallback() -> None:
    record = prepare(
        feature(geometry={"type": "Point", "coordinates": [-75, 40]})
    )

    assert record.hazard is not None
    assert record.hazard["geometry_source"] == "TEXT_CIRCLE"
    assert record.hazard["horizontal_radius_nm"] == 5
    assert record.hazard["circle_center_longitude"] == -75


def test_unresolved_point_only_record_still_creates_version() -> None:
    record = prepare(
        feature(
            geometry={"type": "Point", "coordinates": [-75, 40]},
            text="RWY 04 CLOSED",
        )
    )

    assert record.version["nms_id"] == "nms-1"
    assert record.hazard is None


def test_cancellation_maps_to_cancelled_status() -> None:
    record = prepare(feature(notam_type="C"))

    assert record.version["status"] == "CANCELLED"


def test_payload_keeps_latest_duplicate_nms_version() -> None:
    payload = {
        "data": {
            "geojson": [
                feature(updated="2026-10-27T15:30:00Z"),
                feature(updated="2026-10-27T15:45:00Z"),
            ]
        }
    }

    records = prepare_nms_payload(
        payload,
        fetched_at=NOW,
        request_mode="incremental",
        request_watermark=NOW,
    )

    assert len(records) == 1
    assert records[0].version["valid_from"] == "2026-10-27T15:45:00+00:00"
