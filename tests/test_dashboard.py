"""Unit tests for MCP dashboard result normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "agent-app"))

import dashboard  # noqa: E402
from dashboard import normalize_results, startup_briefing  # noqa: E402


@pytest.mark.parametrize("error", [None, "upstream timeout"])
async def test_startup_briefing_calls_one_mcp_tool(monkeypatch, error) -> None:
    calls = []

    async def call_tool(url, token, name, arguments):
        calls.append((url, token, name, arguments))
        return {"tool": name, "error": error, "data": {"encounters": []}}

    monkeypatch.setattr(dashboard, "_call_tool", call_tool)
    view = await startup_briefing("https://example.test/mcp", "test-token")

    name = "get_flights_approaching_notams"
    assert calls == [
        (
            "https://example.test/mcp",
            "test-token",
            name,
            {"hazard_type": "tfr", "horizon_minutes": 60, "limit": 40},
        )
    ]
    assert view["source_tools"] == [name]
    assert view["metrics"]["flights_shown"] == 0
    assert view["errors"] == ([f"{name}: {error}"] if error else [])


def _hazard(**overrides):
    base = {
        "notam_id": "A001/26",
        "location": "KZDC",
        "text": "AIRSPACE TEST AREA",
        "position": {"lat": 38.9, "lon": -77.0},
        "position_basis": "hazard centroid",
        "hazard_altitude_band": "SFC to 18,000 ft MSL",
    }
    return {**base, **overrides}


def test_normalizes_approaching_flight_and_critical_notam() -> None:
    view = normalize_results(
        [
            {
                "tool": "get_flights_approaching_notams",
                "data": {
                    "evaluated_at": "2026-11-18T15:00:00Z",
                    "data_status": {
                        "flight_positions_as_of": "2026-11-18T14:59:30Z"
                    },
                    "encounters": [
                        {
                            "callsign": "DAL636",
                            "position": {"lat": 38.8, "lon": -77.2},
                            "altitude_ft": 12000,
                            "origin": "KATL",
                            "destination": "KDCA",
                            "status": "projected_entry",
                            "minutes_to_entry": 9,
                            "altitude_certainty": "confirmed",
                            "hazard_altitude_band": "SFC to 18,000 ft MSL",
                            "hazard": _hazard(),
                        }
                    ],
                },
            }
        ],
        "TFR watch",
    )

    assert view["metrics"] == {
        "critical_notams": 1,
        "high_notams": 0,
        "uncertain_notams": 0,
        "flights_shown": 1,
        "notams_shown": 1,
    }
    assert view["flights"][0]["callsign"] == "DAL636"
    assert view["flights"][0]["severity"] == "critical"
    assert view["notams"][0]["callsigns"] == ["DAL636"]
    assert view["data_status"]["flight_positions_as_of"].endswith("Z")


def test_normalizes_route_deviation_near_crossed_notam() -> None:
    view = normalize_results(
        [
            {
                "tool": "find_route_deviations_near_notams",
                "data": {
                    "flights": [
                        {
                            "callsign": "UAL42",
                            "position": {"lat": 39.0, "lon": -76.5},
                            "cross_track_deviation_nm": 12.4,
                            "nearest_active_notam": _hazard(
                                planned_route_crosses_it=True
                            ),
                        }
                    ]
                },
            }
        ],
        "Deviation watch",
    )

    assert view["flights"][0]["detail"] == "12.4 NM off plan"
    assert view["flights"][0]["severity"] == "high"
    assert view["notams"][0]["status"] == "Near deviating flight"


def test_normalizes_route_briefing_and_route_line() -> None:
    view = normalize_results(
        [
            {
                "tool": "get_route_notams",
                "data": {
                    "route": {
                        "origin": "KJFK",
                        "destination": "KLAX",
                        "origin_position": {"lat": 40.64, "lon": -73.78},
                        "destination_position": {"lat": 33.94, "lon": -118.41},
                    },
                    "enroute_hazards": {
                        "hazards": [
                            _hazard(
                                vertical_relation="overlaps",
                                active_at_estimated_pass_time=True,
                            )
                        ]
                    },
                },
            }
        ],
        "KJFK to KLAX",
    )

    assert view["routes"] == [
        {
            "label": "KJFK to KLAX",
            "points": [
                {"lat": 40.64, "lon": -73.78},
                {"lat": 33.94, "lon": -118.41},
            ],
        }
    ]
    assert view["metrics"]["high_notams"] == 1


@pytest.mark.parametrize(
    ("coordinates", "expected_routes"),
    [
        (
            [[-73.78, 40.64], [-76.35, 40.12], [-77.04, 38.85]],
            [
                {
                    "label": "DAL636 filed route",
                    "points": [
                        {"lat": 40.64, "lon": -73.78},
                        {"lat": 40.12, "lon": -76.35},
                        {"lat": 38.85, "lon": -77.04},
                    ],
                }
            ],
        ),
        (None, []),
        ([[-73.78, 40.64]], []),
        ([[-73.78, 40.64], [None, 40.12], [-77.04, 38.85]], []),
    ],
)
def test_live_flight_view_keeps_marker_separate_from_filed_route(
    coordinates, expected_routes
) -> None:
    position = {"lat": 39.82, "lon": -75.42}
    view = normalize_results(
        [
            {
                "tool": "get_flight_briefing",
                "data": {
                    "flight": {"callsign": "DAL636", "position": position},
                    "plan": {"route_coordinates": coordinates},
                },
            }
        ],
        "Show the filed route for live flight DAL636",
    )

    assert view["routes"] == expected_routes
    assert view["flights"][0]["position"] == position


def test_normalizes_search_results_without_flights() -> None:
    view = normalize_results(
        [
            {
                "tool": "search_notams",
                "data": {
                    "notams": [
                        _hazard(
                            has_mapped_hazard_area=False,
                            position={"lat": "38.9", "lon": "-77.0"},
                        )
                    ]
                },
            }
        ],
        "NOTAM search",
    )

    assert view["metrics"]["flights_shown"] == 0
    assert view["metrics"]["notams_shown"] == 1
    assert view["notams"][0]["severity"] == "advisory"
    assert view["notams"][0]["position"] == {"lat": 38.9, "lon": -77.0}


def test_preserves_partial_results_and_reports_tool_errors() -> None:
    view = normalize_results(
        [
            {
                "tool": "search_notams",
                "data": {"notams": [_hazard(has_mapped_hazard_area=True)]},
            },
            {
                "tool": "get_flight_briefing",
                "error": "upstream timeout",
            },
        ],
        "Partial analyst view",
    )

    assert view["metrics"]["high_notams"] == 1
    assert view["errors"] == ["get_flight_briefing: upstream timeout"]
    assert view["source_tools"] == ["search_notams", "get_flight_briefing"]
