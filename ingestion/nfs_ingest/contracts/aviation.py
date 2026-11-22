"""Typed contracts for normalized aviation risk data."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AviationModel(BaseModel):
    """Strict base model shared by persisted aviation entities."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AltitudeReference(StrEnum):
    MSL = "MSL"
    AGL = "AGL"
    FL = "FL"
    STD = "STD"
    SFC = "SFC"
    UNL = "UNL"
    UNBOUNDED = "UNBOUNDED"
    UNKNOWN = "UNKNOWN"


class EncounterState(StrEnum):
    PREDICTED = "PREDICTED"
    APPROACHING = "APPROACHING"
    INSIDE = "INSIDE"
    CLEARED = "CLEARED"
    EXPIRED = "EXPIRED"


class ManeuverType(StrEnum):
    TURN = "TURN"
    CLIMB = "CLIMB"
    DESCENT = "DESCENT"
    SPEED_CHANGE = "SPEED_CHANGE"
    HOLD = "HOLD"
    ROUTE_DEVIATION = "ROUTE_DEVIATION"


class RiskSeverity(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    ADVISORY = "ADVISORY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class WarningStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


class FlightPositionEvent(AviationModel):
    event_id: str
    bronze_event_id: str
    record_index: int = Field(ge=0)
    flight_key: str
    flight_id: str
    gufi: str | None = None
    observed_at: datetime
    received_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_ft: float | None = None
    ground_speed_kts: float | None = Field(default=None, ge=0)
    track_heading_deg: float | None = Field(default=None, ge=0, lt=360)
    vertical_rate_fpm: float | None = None
    origin: str | None = None
    destination: str | None = None
    h3_r7: int | None = None
    source: str = "FAA_SWIM_TFM_R14"
    source_freshness_seconds: float | None = Field(default=None, ge=0)


class NotamHazardVolume(AviationModel):
    hazard_id: str
    notam_version_id: str
    nms_id: str
    notam_id: str | None = None
    location: str | None = None
    effective_start: datetime
    effective_end: datetime | None = None
    geometry_geojson: dict[str, Any]
    geometry_source: str
    lower_altitude_ft: float | None = None
    upper_altitude_ft: float | None = None
    lower_altitude_reference: AltitudeReference = AltitudeReference.UNKNOWN
    upper_altitude_reference: AltitudeReference = AltitudeReference.UNKNOWN
    horizontal_radius_nm: float = Field(default=0, ge=0)
    circle_center_longitude: float | None = Field(default=None, ge=-180, le=180)
    circle_center_latitude: float | None = Field(default=None, ge=-90, le=90)
    h3_resolution: int = Field(default=7, ge=0, le=15)
    h3_cells: list[int] = Field(default_factory=list)
    horizontal_buffer_nm: float = Field(default=0, ge=0)
    parse_confidence: float = Field(default=1, ge=0, le=1)
    parse_warnings: list[str] = Field(default_factory=list)
    source: str = "FAA_NMS"

    @model_validator(mode="after")
    def validate_bounds(self) -> NotamHazardVolume:
        if self.effective_end and self.effective_end <= self.effective_start:
            raise ValueError("effective_end must be after effective_start")
        if (
            self.lower_altitude_ft is not None
            and self.upper_altitude_ft is not None
            and self.lower_altitude_ft > self.upper_altitude_ft
        ):
            raise ValueError("lower_altitude_ft cannot exceed upper_altitude_ft")
        return self


class FlightTrajectorySegment(AviationModel):
    segment_id: str
    flight_key: str
    start_event_id: str
    end_event_id: str | None = None
    start_time: datetime
    end_time: datetime
    path_geojson: dict[str, Any]
    start_altitude_ft: float | None = None
    end_altitude_ft: float | None = None
    min_altitude_ft: float | None = None
    max_altitude_ft: float | None = None
    average_ground_speed_kts: float | None = Field(default=None, ge=0)
    initial_heading_deg: float | None = Field(default=None, ge=0, lt=360)
    final_heading_deg: float | None = Field(default=None, ge=0, lt=360)
    h3_cells_r7: list[int] = Field(default_factory=list)
    is_projected: bool = False
    projection_horizon_seconds: int = Field(default=0, ge=0)


class NotamFlightEncounter(AviationModel):
    encounter_id: str
    flight_key: str
    hazard_id: str
    first_detected_at: datetime
    predicted_entry_at: datetime | None = None
    actual_entry_at: datetime | None = None
    closest_approach_at: datetime | None = None
    cleared_at: datetime | None = None
    state: EncounterState
    horizontal_intersection: bool
    vertical_overlap: bool
    temporal_overlap: bool
    minimum_horizontal_distance_nm: float | None = Field(default=None, ge=0)
    minimum_vertical_separation_ft: float | None = Field(default=None, ge=0)
    time_to_entry_seconds: int | None = None
    rule_version: str


class FlightManeuver(AviationModel):
    maneuver_id: str
    flight_key: str
    encounter_id: str | None = None
    detected_at: datetime
    maneuver_type: ManeuverType
    heading_change_deg: float | None = None
    altitude_change_ft: float | None = None
    speed_change_kts: float | None = None
    cross_track_deviation_nm: float | None = Field(default=None, ge=0)
    distance_to_hazard_nm: float | None = Field(default=None, ge=0)
    avoidance_likelihood: float | None = Field(default=None, ge=0, le=1)
    detector_version: str


class NotamResponseBaseline(AviationModel):
    baseline_id: str
    hazard_cluster_key: str
    route_cluster_key: str | None = None
    aircraft_class: str | None = None
    sample_count: int = Field(ge=1)
    median_lateral_deviation_nm: float = Field(ge=0)
    p90_lateral_deviation_nm: float = Field(ge=0)
    median_altitude_change_ft: float
    median_warning_lead_seconds: float = Field(ge=0)
    maneuver_distribution: dict[str, float]
    window_start: datetime
    window_end: datetime
    model_version: str


class FlightNotamRiskScore(AviationModel):
    risk_id: str
    encounter_id: str
    scored_at: datetime
    valid_until: datetime
    score: float = Field(ge=0, le=100)
    severity: RiskSeverity
    trajectory_conflict_score: float = Field(ge=0, le=1)
    route_deviation_score: float = Field(ge=0, le=1)
    peer_response_score: float = Field(ge=0, le=1)
    data_quality_score: float = Field(ge=0, le=1)
    reasons: list[str]
    rule_version: str
    model_version: str | None = None


class WarningEvent(AviationModel):
    warning_id: str
    risk_id: str
    flight_key: str
    hazard_id: str
    issued_at: datetime
    expires_at: datetime
    severity: RiskSeverity
    status: WarningStatus = WarningStatus.OPEN
    message: str
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
