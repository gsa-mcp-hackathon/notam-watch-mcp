"""SAA AIXM parsing and NOTAM -> special use airspace linking."""

from __future__ import annotations

import json
import math

from nfs_ingest.services.notam_sua import SuaIndex, activation_band, find_sua_references
from nfs_ingest.services.saa_airspace import (
    arc_points,
    dedupe_airspaces,
    destination,
    month_day,
    parse_saa_message,
    timesheet_issues,
)

_SAA = """<?xml version="1.0" encoding="UTF-8"?>
<SaaMessage xmlns="urn:us:gov:dot:faa:aim:saa" xmlns:gml="http://www.opengis.net/gml/3.2">
 <hasMember><Airspace xmlns="http://www.aixm.aero/schema/5.0">
  <timeSlice><AirspaceTimeSlice>
   <designator>R5206</designator><name>R-5206 WEST POINT, NY</name>
   <geometryComponent><AirspaceGeometryComponent>
    <operation>BASE</operation><operationSequence>1</operationSequence>
    <theAirspaceVolume><AirspaceVolume>
     <upperLimit uom="FT">05000</upperLimit>
     <upperLimitReference>MSL</upperLimitReference>
     <lowerLimit uom="FT">GND</lowerLimit>
     <lowerLimitReference>SFC</lowerLimitReference>
     <horizontalProjection><Surface><patches xmlns="http://www.opengis.net/gml/3.2">
      <PolygonPatch><exterior><Ring><curveMember><Curve><segments>
       <LineStringSegment><pos>-74.0 41.4</pos><pos>-73.9 41.4</pos></LineStringSegment>
       <ArcByCenterPoint>
        <pointProperty><Point><pos>-73.95 41.35</pos></Point></pointProperty>
        <radius uom="NM">2.0</radius><startAngle uom="deg">0</startAngle>
        <endAngle uom="deg">-90</endAngle></ArcByCenterPoint>
      </segments></Curve></curveMember></Ring></exterior></PolygonPatch>
     </patches></Surface></horizontalProjection>
    </AirspaceVolume></theAirspaceVolume>
   </AirspaceGeometryComponent></geometryComponent>
   <geometryComponent><AirspaceGeometryComponent>
    <operation>SUBTR</operation><operationSequence>2</operationSequence>
    <theAirspaceVolume><AirspaceVolume>
     <horizontalProjection><Surface><patches xmlns="http://www.opengis.net/gml/3.2">
      <PolygonPatch><exterior><Ring><curveMember><Curve><segments>
       <CircleByCenterPoint><pointProperty><Point><pos>-73.95 41.38</pos></Point>
        </pointProperty><radius uom="NM">0.5</radius></CircleByCenterPoint>
      </segments></Curve></curveMember></Ring></exterior></PolygonPatch>
     </patches></Surface></horizontalProjection>
    </AirspaceVolume></theAirspaceVolume>
   </AirspaceGeometryComponent></geometryComponent>
  </AirspaceTimeSlice></timeSlice>
 </Airspace></hasMember>
 <suaType>RA</suaType>
 <Timesheet><timeReference>UTC</timeReference><day>MON</day><dayTil>FRI</dayTil>
  <startTime>12:00</startTime><endTime>22:00</endTime></Timesheet>
</SaaMessage>""".encode()


def _nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    return math.hypot((a[0] - b[0]) * 60 * math.cos(lat), (a[1] - b[1]) * 60)


def test_parse_saa_message_components_limits_schedule() -> None:
    (record,) = parse_saa_message(_SAA, source_file="R-5206 WEST POINT, NY.xml")
    assert record["designator_normalized"] == "R5206"
    assert record["sua_type"] == "RA"
    assert (record["lower_ft"], record["lower_reference"]) == (0.0, "AGL")
    assert (record["upper_ft"], record["upper_reference"]) == (5000.0, "MSL")
    components = json.loads(record["components_json"])
    assert [c["operation"] for c in components] == ["BASE", "SUBTR"]
    ring = components[0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]
    (sheet,) = json.loads(record["schedule_json"])
    assert sheet["day_til"] == "FRI"
    assert sheet["issues"] == []  # UTC, MON-FRI 12:00-22:00, no dates


def _sheet(**fields) -> dict:
    base = {"time_reference": "UTC", "day": "ANY", "start_time": "07:00",
            "end_time": "22:00", "start_md": "01-01", "end_md": "12-31",
            "start_date": "01-01", "end_date": "31-12"}
    return {**base, **fields}


def test_month_day_accepts_faa_and_aixm51_forms() -> None:
    assert month_day("31-12") == "12-31"  # FAA SAA 5.0: DD-MM
    assert month_day("01-09") == "09-01"
    assert month_day("--11-05") == "11-05"  # AIXM 5.1 DateMonthDayType
    assert month_day("29-02") == "02-29"
    assert month_day("31-04") is None and month_day("SOMEDAY") is None
    assert month_day(None) is None


def test_clean_timesheet_has_no_issues() -> None:
    assert timesheet_issues(_sheet()) == []
    assert timesheet_issues(_sheet(day="MON", day_til="FRI")) == []  # whole year
    assert timesheet_issues(_sheet(end_time="24:00")) == []  # end-of-day rule


def test_local_time_and_reference_rules() -> None:
    assert timesheet_issues(_sheet(time_offset_hr=-7.0)) == ["local_time_offset"]
    assert timesheet_issues(_sheet(time_offset_hr=0.0)) == []
    assert "time_reference_not_utc" in timesheet_issues(_sheet(time_reference="LT"))
    assert timesheet_issues(_sheet(daylight_saving_adjust="YES")) == [
        "daylight_saving_adjust"]
    assert timesheet_issues(_sheet(daylight_saving_adjust="NO")) == []


def test_day_value_rules() -> None:
    # LA VETA HIGH MOA uses WORK_DAY.
    assert timesheet_issues(_sheet(day="WORK_DAY")) == ["prohibited_day_value"]
    assert "prohibited_day_value" in timesheet_issues(_sheet(day="MON", day_til="HOL"))
    # A seasonal range (Sep 1 - Apr 30) combined with a weekday must use ANY.
    seasonal = {"start_date": "01-09", "end_date": "30-04", "start_md": "09-01",
                "end_md": "04-30"}
    assert timesheet_issues(_sheet(day="SAT", **seasonal)) == [
        "weekday_with_date_range"]
    assert timesheet_issues(_sheet(day="ANY", **seasonal)) == []
    assert timesheet_issues(_sheet(start_date="31-04", start_md=None)) == [
        "invalid_date"]


def test_time_rules() -> None:
    assert timesheet_issues(_sheet(start_time="08:00", end_time="01:00")) == [
        "overnight_span"]
    assert timesheet_issues(_sheet(start_time="07:00", end_time="00:00")) == [
        "midnight_end_as_0000"]
    assert timesheet_issues(_sheet(start_time="22:00", end_time="24:00")) == []
    # LIVE OAK MOA codes WED 06:00 to THU 02:00 with dayTil: correct, no issue.
    assert timesheet_issues(_sheet(day="WED", day_til="THU", start_time="06:00",
                                   end_time="02:00")) == []
    assert timesheet_issues(_sheet(day="MON", day_til="TUE", start_time="22:00",
                                   end_time="00:00")) == []  # end-of-day rule
    assert timesheet_issues(_sheet(start_time="7am")) == ["invalid_time"]
    # Solar events stand in for clock times (A-260: SR-SS).
    solar = _sheet(start_time=None, end_time=None, start_event="SR", end_event="SS")
    assert timesheet_issues(solar) == []
    assert timesheet_issues(_sheet(end_time=None)) == ["missing_end"]


def test_parse_reads_events_offset_and_flags_issues() -> None:
    xml = _SAA.replace(
        b"<Timesheet><timeReference>UTC</timeReference><day>MON</day><dayTil>FRI</dayTil>\n"
        b"  <startTime>12:00</startTime><endTime>22:00</endTime></Timesheet>",
        b"<Timesheet><timeReference>UTC</timeReference><startDate>01-01</startDate>"
        b"<endDate>31-12</endDate><day>ANY</day><startEvent>SR</startEvent>"
        b"<endEvent>SS</endEvent><extension><TimesheetExtension>"
        b'<timeOffset uom="HR">-7.0</timeOffset></TimesheetExtension></extension>'
        b"</Timesheet>",
    )
    assert xml != _SAA
    (record,) = parse_saa_message(xml)
    (sheet,) = json.loads(record["schedule_json"])
    assert (sheet["start_event"], sheet["end_event"]) == ("SR", "SS")
    assert (sheet["start_md"], sheet["end_md"]) == ("01-01", "12-31")
    assert sheet["time_offset_hr"] == -7.0
    assert sheet["issues"] == ["local_time_offset"]


def test_arc_uses_gml_angles_counter_clockwise_from_east() -> None:
    center = (-73.95, 41.35)
    points = arc_points(center, 2.0, 0.0, 90.0)
    # 0 deg = east of center, 90 deg = north of center.
    assert points[0][0] > center[0] and abs(points[0][1] - center[1]) < 1e-3
    assert points[-1][1] > center[1] and abs(points[-1][0] - center[0]) < 1e-3
    assert all(abs(_nm(p, center) - 2.0) < 0.02 for p in points)


def test_destination_matches_known_distance() -> None:
    lon, lat = destination(-77.0, 38.9, 0.0, 60.0)
    assert abs(lat - 39.9) < 0.01 and abs(lon + 77.0) < 1e-6


def test_dedupe_prefers_airspace_own_file() -> None:
    own = {"designator_normalized": "R4810", "name": "R-4810 DESERT MOUNTAINS, NV",
           "source_file": "R-4810 DESERT MOUNTAINS, NV.xml"}
    copy = {**own, "source_file": "R-4812 SAND SPRINGS, NV.xml"}
    (kept,) = dedupe_airspaces([copy, own])
    assert kept["source_file"] == own["source_file"]


INDEX = SuaIndex.build(
    [
        {"designator_normalized": "R5107K", "name_code": "R-5107K WSMR",
         "sua_type": "RA"},
        {"designator_normalized": "R6714A", "name_code": "", "sua_type": "RA"},
        {"designator_normalized": "R6714F", "name_code": "", "sua_type": "RA"},
        {"designator_normalized": "R6714B", "name_code": "", "sua_type": "RA"},
        {"designator_normalized": "R2206A", "name_code": "", "sua_type": "RA"},
        {"designator_normalized": "MFALLONN2", "name_code": "FALLON NORTH 2 MOA",
         "sua_type": "MOA"},
        {"designator_normalized": "MONEILL", "name_code": "O'NEILL MOA",
         "sua_type": "MOA"},
    ]
)


def _designators(text: str) -> list[str]:
    return sorted(r["designator"] for r in find_sua_references(text, INDEX))


def test_activation_notams_link_to_sua() -> None:
    assert _designators("!SUAC 10/483 ZAB AIRSPACE R5107K ACT SFC-FL300") == ["R5107K"]
    assert _designators(
        "!SUAW 10/237 ZOA AIRSPACE FALLON NORTH 2 MOA ACT 100FT UP TO BUT NOT "
        "INCLUDING FL180"
    ) == ["MFALLONN2"]
    assert _designators("!SUAC 10/778 ZMP AIRSPACE ONEILL MOA ACT 500FT-FL180") == [
        "MONEILL"
    ]


def test_letter_lists_and_groups_expand() -> None:
    assert _designators("AIRSPACE TRAINING WITHIN R6714 A/F") == ["R6714A", "R6714F"]
    assert _designators("AIRSPACE R6714 ACT SFC-5000FT") == [
        "R6714A", "R6714B", "R6714F"]


def test_radials_exclusions_and_passing_mentions_do_not_link() -> None:
    assert _designators("AIRSPACE HOLD ON HCM VORTAC R-082 ACT") == []
    assert _designators(
        "AIRSPACE TFR. THESE RESTRICTIONS EXCLUDE THAT PORTION OF AIRSPACE WI "
        "R-2206A WHEN ACTIVE"
    ) == []
    assert _designators("IAP LOC RWY 3 MISSED APPROACH VIA R5107K") == []


def test_activation_band_parsing() -> None:
    assert activation_band("AIRSPACE R5107K ACT SFC-FL300") == {
        "lower_ft": 0.0, "lower_reference": "AGL",
        "upper_ft": 30000.0, "upper_reference": "STD"}
    band = activation_band("ACT 6000FT UP TO BUT NOT INCLUDING FL180")
    assert band["lower_ft"] == 6000.0 and band["upper_ft"] == 17900.0
    band = activation_band("ACT 500FT AGL-4999FT AGL")
    assert band["lower_reference"] == band["upper_reference"] == "AGL"
    assert activation_band("AIRSPACE W532E ACT") is None
