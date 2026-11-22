"""Unit tests for the gold-layer evidence logic (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.tool_resources import queries as q
from server.tool_resources.queries.common import hazard_summary

T0 = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)


def hazard(**overrides):
    base = {
        "hazard_id": "h1",
        "notam_id": "10/001",
        "location": "KZDC",
        "q_code": None,
        "hazard_class": "airspace",
        "geometry_source": "TEXT_CIRCLE",
        "lower_altitude_ft": 0.0,
        "upper_altitude_ft": 18000.0,
        "lower_altitude_reference": "SFC",
        "upper_altitude_reference": "MSL",
        "effective_start": T0 - timedelta(hours=1),
        "effective_end": T0 + timedelta(hours=3),
        "parse_confidence": 0.8,
        "text": "AIRSPACE TEST AREA",
    }
    return {**base, **overrides}


def test_vertical_relation_overlap_above_below() -> None:
    h = hazard()
    assert q.vertical_relation(10000, h)["relation"] == "overlaps"
    assert q.vertical_relation(35000, h)["relation"] == "above"
    floor = hazard(lower_altitude_ft=20000.0, lower_altitude_reference="MSL")
    assert q.vertical_relation(5000, floor)["relation"] == "below"


def test_vertical_relation_buffer_and_unbounded_and_unknown() -> None:
    # Within the 1,000 ft buffer of the ceiling counts as overlapping.
    assert q.vertical_relation(18800, hazard())["relation"] == "overlaps"
    unbounded = hazard(upper_altitude_ft=None, upper_altitude_reference="UNBOUNDED")
    assert q.vertical_relation(45000, unbounded)["relation"] == "overlaps"
    no_ceiling = hazard(upper_altitude_ft=None, upper_altitude_reference="UNKNOWN")
    assert q.vertical_relation(30000, no_ceiling)["relation"] == "unknown"
    assert q.vertical_relation(None, hazard())["relation"] == "unknown"


def test_agl_bands_remain_unknown_without_terrain() -> None:
    h = hazard(upper_altitude_ft=400.0, upper_altitude_reference="AGL")
    result = q.vertical_relation(300, h)
    assert result["relation"] == "unknown"
    assert result["approximate"] is False
    assert result["unresolved_reason"] == "AGL band requires terrain elevation"
    assert result["band"] == "SFC to 400 ft AGL"


def test_feature_notams_have_no_vertical_relation() -> None:
    for cls in ("aerodrome", "navaid"):
        result = q.vertical_relation(3000, hazard(hazard_class=cls))
        assert result["relation"] == "not_applicable"


def test_band_text_flight_levels_and_unknown_reference() -> None:
    h = hazard(
        lower_altitude_ft=18000.0,
        lower_altitude_reference="STD",
        upper_altitude_ft=45000.0,
        upper_altitude_reference="STD",
    )
    assert q.band_text(h) == "FL180 to FL450"
    h2 = hazard(upper_altitude_ft=16000.0, upper_altitude_reference="UNKNOWN")
    assert q.band_text(h2) == "SFC to 16,000 ft (reference unknown)"


def test_expected_altitude_profile() -> None:
    kw = {"start_ft": 0, "cruise_ft": 35000, "end_ft": 600}
    assert q.expected_altitude(10, 700, **kw) == 3000
    assert q.expected_altitude(350, 700, **kw) == 35000
    assert q.expected_altitude(690, 700, **kw) == 3600
    # Short hop never reaches cruise: peak limited by climb and descent.
    assert q.expected_altitude(50, 100, **kw) < 35000


def test_assess_route_hazards_orders_conflicts_first() -> None:
    rows = [
        hazard(hazard_id="far", along_route_nm=300, off_route_nm=9.0,
               route_crosses=False, upper_altitude_ft=400.0,
               upper_altitude_reference="AGL"),
        hazard(hazard_id="hit", along_route_nm=5, off_route_nm=0.0,
               route_crosses=True),
    ]
    assessed = q.assess_route_hazards(
        rows,
        route_length_nm=700,
        departure_time=T0,
        speed_kts=420,
        start_altitude_ft=0,
        cruise_altitude_ft=35000,
        end_altitude_ft=0,
    )
    first, second = assessed
    assert first["route_crosses_hazard"] is True
    assert first["vertical_relation"] == "overlaps"  # ~1,500 ft in the climb
    assert first["flight_phase"] == "climb"
    assert first["active_at_estimated_pass_time"] is True
    assert second["vertical_relation"] == "unknown"
    assert second["estimated_pass_time"].startswith("2026-10-29T02:42")
    assert "_score" not in first


def test_is_active_respects_open_end() -> None:
    assert q.is_active(hazard(effective_end=None), T0 + timedelta(days=400))
    assert not q.is_active(hazard(), T0 + timedelta(hours=5))


def test_parse_time_defaults_and_naive_utc() -> None:
    assert q.parse_time(None, T0) == T0
    assert q.parse_time("2026-10-29T03:00:00", T0) == T0 + timedelta(hours=1)
    assert q.parse_time("2026-10-29T03:00:00Z", T0) == T0 + timedelta(hours=1)


class FakeDB:
    """Answers queries by matching a distinctive SQL fragment."""

    def __init__(self, answers: dict[str, list[dict]]):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, sql: str, params: dict) -> list[dict]:
        self.calls.append((sql, params))
        for fragment, rows in self.answers.items():
            if fragment in sql:
                return rows
        return []


def test_flight_briefing_preserves_filed_route_coordinates() -> None:
    coordinates = [[-73.78, 40.64], [-76.35, 40.12], [-77.04, 38.85]]
    db = FakeDB(
        {
            "FROM aviation.flights": [
                {
                    "callsign": "DAL636",
                    "observed_at": T0,
                    "lat": 39.82,
                    "lon": -75.42,
                    "route_len_nm": 190.0,
                    "route_coordinates": coordinates,
                }
            ]
        }
    )

    result = q.flight_briefing(callsign="dal636", corridor_nm=10, limit=5, fetch=db)

    assert result["plan"]["route_coordinates"] == coordinates
    assert result["flight"]["position"] == {"lat": 39.82, "lon": -75.42}


def test_route_notams_reports_unknown_airport() -> None:
    result = q.route_notams(
        origin_code="KJFK",
        destination_code="ZZZZ",
        departure_time=T0,
        cruise_altitude_ft=None,
        corridor_nm=10,
        callsign=None,
        include_obstacles=False,
        limit=5,
        fetch=FakeDB({}),
    )
    assert "error" in result and "KJFK" in result["error"]


def test_resolve_airport_maps_icao_to_faa_id() -> None:
    db = FakeDB({"FROM aviation.airports": [{"arpt_id": "JFK"}]})
    assert q.resolve_airport("kjfk", db) == {"arpt_id": "JFK"}
    _, params = db.calls[0]
    assert params == {"ident": "KJFK", "faa": "JFK"}


def test_hazard_summary_includes_representative_map_position() -> None:
    result = hazard_summary(hazard(hz_lat=38.8977, hz_lon=-77.0365))
    assert result["position"] == {"lat": 38.8977, "lon": -77.0365}
    assert result["position_basis"] == "point on mapped hazard area"


def test_search_notams_builds_safe_patterns() -> None:
    db = FakeDB({})
    q.search_notams(
        location="ORD",
        text_contains=" rwy 04L ",
        q_code_prefix="qmr",
        near_lon=None,
        near_lat=None,
        radius_nm=25,
        active_at=T0,
        include_obstacles=False,
        limit=5,
        fetch=db,
    )
    _, params = db.calls[0]
    assert params["codes"] == ["ORD", "KORD"]
    assert params["text"] == "%rwy 04L%"
    assert params["q"] == "QMR%"


def test_search_notams_includes_anchor_position() -> None:
    row = {
        "notam_id": "A001/26",
        "location": "KDCA",
        "q_code": "QFMAU",
        "effective_start": T0 - timedelta(hours=1),
        "effective_end": T0 + timedelta(hours=1),
        "hazard_count": 0,
        "lat": 38.8512,
        "lon": -77.0402,
        "distance_nm": 0.0,
        "distance_basis": "anchor_point",
        "schedule": None,
        "text": "TEST NOTAM",
    }
    result = q.search_notams(
        location="DCA",
        text_contains=None,
        q_code_prefix=None,
        near_lon=None,
        near_lat=None,
        radius_nm=25,
        active_at=T0,
        include_obstacles=False,
        limit=5,
        fetch=FakeDB({"FROM aviation.notams n": [row]}),
    )
    assert result["notams"][0]["position"] == {
        "lat": 38.8512,
        "lon": -77.0402,
    }
    assert result["notams"][0]["position_basis"] == "NOTAM anchor point"


def test_flights_approaching_filters_vertically_clear() -> None:
    base = {
        "callsign": "AAL1",
        "lat": 38.9,
        "lon": -77.0,
        "observed_at": T0,
        "ground_speed_kts": 300.0,
        "inside_now": False,
        "entry_frac": 0.5,
        "path_len_m": 37040.0,
        "planned_altitude_ft": 35000.0,
        "projection_basis": "planned_route",
        "distance_now_nm": 10.0,
    }
    low = {**hazard(upper_altitude_ft=400.0, upper_altitude_reference="MSL"),
           **base, "altitude_ft": 30000.0}
    agl = {**low, "callsign": "DAL3", "upper_altitude_reference": "AGL"}
    tall = {**hazard(), **base, "callsign": "UAL2", "altitude_ft": 12000.0}
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}], "WITH hz AS": [low, tall, agl]})
    result = q.flights_approaching_hazards(
        notam_id=None,
        center_lon=-77.0,
        center_lat=38.9,
        radius_nm=50,
        horizon_minutes=30,
        include_obstacles=False,
        limit=10,
        fetch=db,
    )
    assert result["excluded_vertically_clear"] == 1
    encounter, unresolved = result["encounters"]
    assert encounter["callsign"] == "UAL2"
    assert encounter["distance_to_entry_nm"] == 10.0
    assert encounter["minutes_to_entry"] == 2.0
    assert unresolved["callsign"] == "DAL3"
    assert unresolved["altitude_certainty"] == "altitude_unknown"


def _watch(db, **overrides):
    args = {
        "notam_id": None,
        "center_lon": None,
        "center_lat": None,
        "radius_nm": 50,
        "horizon_minutes": 30,
        "include_obstacles": False,
        "limit": 10,
        "fetch": db,
    }
    return q.flights_approaching_hazards(**{**args, **overrides})


def test_nationwide_tfr_scan_is_one_query_with_type_filter() -> None:
    row = {
        **hazard(q_code="QRTCA"),
        "callsign": "SWA1",
        "lat": 32.9,
        "lon": -97.0,
        "observed_at": T0,
        "ground_speed_kts": 300.0,
        "inside_now": True,
        "entry_frac": None,
        "path_len_m": 1000.0,
        "altitude_ft": 5000.0,
        "planned_altitude_ft": None,
        "projection_basis": "track_extrapolation",
        "distance_now_nm": 0.0,
    }
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}], "WITH hz AS": [row]})
    result = _watch(db, hazard_type="TFR")
    (sql, params) = next(c for c in db.calls if "WITH hz AS" in c[0])
    assert "n.category LIKE 'QRT%%'" in sql
    assert "TEMPORARY FLIGHT RESTRICTION" in sql
    assert "ST_Expand" in sql  # bounding-box prefilter keeps it under timeout
    assert params["center"] is None
    assert params["max_hazards"] == q.MAX_WATCH_HAZARDS
    assert result["scope"] == {
        "hazard_type": "tfr",
        "area": "nationwide",
        "flights_affected": 1,
    }
    assert result["encounters"][0]["status"] == "inside_now"


def test_identical_areas_merge_and_altitude_certainty_split() -> None:
    def row(notam_id, callsign, geom_key, **band):
        return {
            **hazard(notam_id=notam_id, location="KZFW", **band),
            "flight_key": callsign,
            "callsign": callsign,
            "geom_key": geom_key,
            "lat": 33.1,
            "lon": -97.2,
            "observed_at": T0,
            "ground_speed_kts": 250.0,
            "inside_now": True,
            "entry_frac": None,
            "path_len_m": 1000.0,
            "altitude_ft": 30000.0,
            "planned_altitude_ft": None,
            "projection_basis": "track_extrapolation",
            "distance_now_nm": 0.0,
        }

    unknown = {"upper_altitude_ft": None, "upper_altitude_reference": "UNKNOWN"}
    tall = {"upper_altitude_ft": None, "upper_altitude_reference": "UNBOUNDED"}
    rows = [
        # Three NOTAMs publishing the same area, one flight -> one encounter.
        row("6/5874", "AAL1", "g-dallas", **unknown),
        row("6/7546", "AAL1", "g-dallas", **unknown),
        row("6/9578", "AAL1", "g-dallas", **unknown),
        # A different area with a parsed band -> confirmed.
        row("6/2480", "UAL2", "g-maine", **tall),
    ]
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}], "WITH hz AS": rows})
    result = _watch(db, hazard_type="tfr")
    assert result["merged_duplicate_area_encounters"] == 2
    assert result["total_encounters"] == 2
    assert result["counts"] == {
        "confirmed": {"encounters": 1, "flights": 1},
        "altitude_unknown": {"encounters": 1, "flights": 1},
    }
    first, second = result["encounters"]
    assert first["callsign"] == "UAL2" and first["altitude_certainty"] == "confirmed"
    assert second["altitude_certainty"] == "altitude_unknown"
    assert second["hazard"]["same_area_notams"] == ["6/7546 KZFW", "6/9578 KZFW"]


def test_sua_encounter_gets_published_times_at_entry() -> None:
    row = {
        **hazard(hazard_id="sua:v1:R5206", geometry_source="SUA_REFERENCE",
                 notam_id="6/0001", location="KZNY"),
        "flight_key": "N1", "callsign": "N1", "geom_key": "g", "lat": 41.4,
        "lon": -73.95, "observed_at": T0, "ground_speed_kts": 120.0,
        "inside_now": True, "entry_frac": None, "path_len_m": 1000.0,
        "altitude_ft": 3000.0, "planned_altitude_ft": None,
        "projection_basis": "track_extrapolation", "distance_now_nm": 0.0,
    }
    sua = {"designator": "R5206", "lat": 41.38, "lon": -73.95, "schedule": [
        {"day": "ANY", "start_time": "00:00", "end_time": "24:00",
         "time_offset_hr": -5.0}]}
    db = FakeDB({
        "max(observed_at) AS t": [{"t": T0}],
        "WITH hz AS": [row],
        "FROM aviation.sua_airspaces WHERE designator = ANY": [sua],
    })
    result = _watch(db, center_lon=-73.95, center_lat=41.4)
    (encounter,) = result["encounters"]
    published = encounter["hazard"]["sua_published_times"]
    assert published["designator"] == "R5206"
    assert published["in_published_times"] is True
    sua_calls = [p for sql, p in db.calls if "aviation.sua_airspaces" in sql]
    assert sua_calls == [{"designators": ["R5206"]}]  # one batched lookup


def test_non_sua_scan_makes_no_sua_lookup() -> None:
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}]})
    _watch(db, hazard_type="tfr")
    assert not any("aviation.sua_airspaces" in sql for sql, _ in db.calls)


def test_nationwide_scan_requires_a_cheap_hazard_type() -> None:
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}]})
    for hazard_type in (None, "uas", "any"):
        try:
            _watch(db, hazard_type=hazard_type)
        except ValueError as exc:
            assert "nationwide" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {hazard_type}")
    # No query ran for a rejected scan.
    assert not any("WITH hz AS" in sql for sql, _ in db.calls)


def test_hazard_type_with_area_anchor_and_unknown_type() -> None:
    db = FakeDB({"max(observed_at) AS t": [{"t": T0}]})
    result = _watch(db, hazard_type="uas", center_lon=-77.0, center_lat=38.9)
    (sql, _) = next(c for c in db.calls if "WITH hz AS" in c[0])
    assert "QWU%%" in sql
    assert result["scope"]["area"] == "filtered"
    assert q.hazard_type_predicate(None) == "TRUE"
    try:
        q.hazard_type_predicate("volcano")
    except ValueError as exc:
        assert "tfr" in str(exc)
    else:
        raise AssertionError("expected ValueError")
