"""Validation tests for normalized aviation data contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nfs_ingest.contracts.aviation import (
    AltitudeReference,
    FlightPositionEvent,
    NotamHazardVolume,
)

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


def test_flight_position_rejects_invalid_coordinates() -> None:
    with pytest.raises(ValidationError):
        FlightPositionEvent(
            event_id="position-1",
            bronze_event_id="bronze-1",
            record_index=0,
            flight_key="GUFI-1",
            flight_id="AAL123",
            observed_at=NOW,
            received_at=NOW,
            latitude=95,
            longitude=-97,
        )


def test_notam_hazard_rejects_inverted_time_and_altitude_bounds() -> None:
    with pytest.raises(ValidationError):
        NotamHazardVolume(
            hazard_id="hazard-1",
            notam_version_id="version-1",
            nms_id="nms-1",
            effective_start=NOW,
            effective_end=NOW - timedelta(minutes=1),
            geometry_geojson={"type": "Polygon", "coordinates": []},
            geometry_source="AIXM",
            lower_altitude_ft=18000,
            upper_altitude_ft=10000,
        )


def test_notam_hazard_accepts_open_ended_valid_volume() -> None:
    volume = NotamHazardVolume(
        hazard_id="hazard-1",
        notam_version_id="version-1",
        nms_id="nms-1",
        effective_start=NOW,
        geometry_geojson={"type": "Polygon", "coordinates": []},
        geometry_source="AIXM",
        lower_altitude_ft=0,
        upper_altitude_ft=18000,
        h3_cells=[608693241318998015],
    )

    assert volume.effective_end is None
    assert volume.h3_resolution == 7


def test_notam_hazard_supports_normalized_flight_level_and_circle() -> None:
    volume = NotamHazardVolume(
        hazard_id="hazard-circle",
        notam_version_id="version-1",
        nms_id="nms-1",
        effective_start=NOW,
        geometry_geojson={"type": "Polygon", "coordinates": []},
        geometry_source="TEXT_CIRCLE",
        lower_altitude_ft=18000,
        lower_altitude_reference=AltitudeReference.STD,
        upper_altitude_reference=AltitudeReference.UNBOUNDED,
        horizontal_radius_nm=5,
        circle_center_longitude=-75,
        circle_center_latitude=40,
    )

    assert volume.horizontal_radius_nm == 5
    assert volume.upper_altitude_reference == AltitudeReference.UNBOUNDED
