"""Tests for SWIM micro-batch preparation and receive/write/ack ordering."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest

from nfs_ingest.config import settings
from nfs_ingest.services import swim_pipeline
from nfs_ingest.services.swim_pipeline import (
    envelope_from_message,
    flight_key,
    prepare_batch,
    stream_messages,
)

NOW = datetime(2026, 10, 28, 5, 0, tzinfo=UTC)

TRACK_XML = """
<tfmDataService>
  <fltdMessage acid="AAL123" depArpt="KDFW" arrArpt="KJFK" msgType="trackInformation"
      flightRef="99887766" sourceTimeStamp="2026-10-28T04:59:30Z">
    <position>
      <latitudeDMS degrees="32" minutes="54" seconds="0" direction="NORTH"/>
      <longitudeDMS degrees="97" minutes="0" seconds="0" direction="WEST"/>
    </position>
    <simpleAltitude>350</simpleAltitude>
    <speed>420</speed>
  </fltdMessage>
  <fltdMessage acid="UAL9" msgType="flightPlanInformation"
      sourceTimeStamp="2026-10-28T04:59:00Z"/>
</tfmDataService>
""".strip()


class FakeMessage:
    def __init__(self, payload: str, app_id: str = "m-1") -> None:
        self.payload = payload
        self.app_id = app_id

    def get_payload_as_string(self) -> str:
        return self.payload

    def get_payload_as_bytes(self) -> bytes:
        return self.payload.encode()

    def get_destination_name(self) -> str:
        return "tfms/flight"

    def get_application_message_id(self) -> str:
        return self.app_id

    def get_properties(self) -> dict:
        return {}


def test_prepare_batch_keeps_bronze_and_extracts_valid_positions() -> None:
    envelope = envelope_from_message(FakeMessage(TRACK_XML), NOW)

    batch = prepare_batch([envelope])

    assert len(batch.bronze) == 1
    assert batch.bronze[0]["parsed_record_count"] == 2
    assert len(batch.positions) == 1
    position = batch.positions[0]
    assert position["flight_key"] == "FREF:99887766"
    assert position["latitude"] == pytest.approx(32.9)
    assert position["longitude"] == pytest.approx(-97.0)
    assert position["altitude_ft"] == 35000.0
    assert position["source_freshness_seconds"] == 30.0
    assert batch.stats["records_without_position"] == 1
    assert batch.stats["msg_type:trackInformation"] == 1


# Real R14 shapes (namespaces as in the feed), trimmed to the relevant parts.
PLAN_XML = """
<ds:tfmDataService xmlns:ds="urn:ds" xmlns:fdm="urn:fdm" xmlns:nxcm="urn:nxcm">
  <fdm:fltdOutput>
    <fdm:fltdMessage acid="DAL636" depArpt="KJFK" arrArpt="PHNL"
        msgType="FlightModify" flightRef="143331572"
        sourceTimeStamp="2026-10-28T13:50:00Z">
      <nxcm:qualifiedAircraftId aircraftCategory="JET" userCategory="COMMERCIAL">
        <nxcm:aircraftId>DAL636</nxcm:aircraftId>
      </nxcm:qualifiedAircraftId>
      <nxcm:aircraftModel>b763</nxcm:aircraftModel>
    </fdm:fltdMessage>
    <fdm:fltdMessage acid="N123AB" msgType="flightPlanInformation"
        flightRef="143331999" sourceTimeStamp="2026-10-28T13:51:00Z">
      <nxcm:aircraftSpecification aircraftEngineClass="JET" equipmentQualifier="L"
          numberOfAircraft="1" specialAircraftQualifier="HEAVY JET">B744
      </nxcm:aircraftSpecification>
    </fdm:fltdMessage>
    <fdm:fltdMessage acid="AAL1" msgType="FlightTimes"
        sourceTimeStamp="2026-10-28T13:52:00Z">
      <nxcm:aircraftModel>A321</nxcm:aircraftModel>
    </fdm:fltdMessage>
  </fdm:fltdOutput>
</ds:tfmDataService>
""".strip()


def test_parser_extracts_aircraft_type_from_flight_plan_messages() -> None:
    from nfs_ingest.services.swim_parser import parse_tfm_xml_messages

    modify, plan, _ = parse_tfm_xml_messages(PLAN_XML)

    assert modify["aircraft_type"] == "B763"
    assert modify["aircraft_category"] == "JET"
    assert modify["user_category"] == "COMMERCIAL"
    assert modify["aircraft_engine_class"] == "JET"
    assert plan["aircraft_type"] == "B744"
    assert plan["equipment_qualifier"] == "L"
    assert plan["special_aircraft_qualifier"] == "HEAVY JET"
    assert plan["aircraft_category"] == ""


def test_aircraft_rows_are_keyed_by_flight_ref() -> None:
    batch = prepare_batch([envelope_from_message(FakeMessage(PLAN_XML), NOW)])

    by_ref = {row["flight_ref"]: row for row in batch.aircraft}

    # The FlightTimes record has no flightRef, so it cannot join to tracks.
    assert set(by_ref) == {"143331572", "143331999"}
    assert by_ref["143331572"]["aircraft_type"] == "B763"
    assert by_ref["143331572"]["origin"] == "KJFK"
    assert by_ref["143331999"]["special_aircraft_qualifier"] == "HEAVY JET"
    assert batch.positions == []
    assert batch.stats["aircraft_type_records"] == 2


def test_positions_carry_flight_ref_and_msg_type() -> None:
    batch = prepare_batch([envelope_from_message(FakeMessage(TRACK_XML), NOW)])

    assert batch.positions[0]["flight_ref"] == "99887766"
    assert batch.positions[0]["msg_type"] == "trackInformation"
    assert batch.aircraft == []


ROUTE_XML = """
<ds:tfmDataService xmlns:ds="urn:ds" xmlns:fdm="urn:fdm" xmlns:nxcm="urn:nxcm"
    xmlns:nxce="urn:nxce">
  <fdm:fltdOutput>
    <fdm:fltdMessage acid="KOW102" depArpt="KIAD" arrArpt="KSJC"
        msgType="flightPlanInformation" flightRef="143414921"
        sourceTimeStamp="2026-10-29T02:22:17Z">
      <fdm:flightPlanInformation>
        <nxcm:qualifiedAircraftId aircraftCategory="JET" userCategory="COMMERCIAL">
          <nxce:gufi>KW085345tO</nxce:gufi>
        </nxcm:qualifiedAircraftId>
        <nxcm:flightAircraftSpecs equipmentQualifier="L">C750</nxcm:flightAircraftSpecs>
        <nxcm:speed><nxce:filedTrueAirSpeed>495</nxce:filedTrueAirSpeed></nxcm:speed>
        <nxcm:altitude><nxce:requestedAltitude>
          <nxce:simpleAltitude>430</nxce:simpleAltitude>
        </nxce:requestedAltitude></nxcm:altitude>
        <nxcm:routeOfFlight legacyFormat="KIAD.MCRAY2.MCRAY.Q178..KSJC/0457"/>
        <nxcm:ncsmRouteData>
          <nxcm:etd etdType="PROPOSED" timeValue="2026-10-29T03:40:00Z"/>
          <nxcm:eta etaType="ESTIMATED" timeValue="2026-10-29T08:39:55Z"/>
          <nxcm:dp routeName="MCRAY2" routeType="DIRECT"/>
          <nxcm:star routeName="RAZRR5" routeType="STAR"/>
          <nxcm:flightTraversalData2>
            <nxce:fix sequenceNumber="1">IAD</nxce:fix>
            <nxce:fix elapsedTime="817" sequenceNumber="2">MCRAY</nxce:fix>
            <nxce:waypoint latitudeDecimal="38.95" longitudeDecimal="-77.46"
                sequenceNumber="1"/>
            <nxce:waypoint elapsedTime="600" latitudeDecimal="39.20"
                longitudeDecimal="-78.10" sequenceNumber="3"/>
            <nxce:waypoint elapsedTime="300" latitudeDecimal="39.05"
                longitudeDecimal="-77.80" sequenceNumber="2"/>
            <nxce:center sequenceNumber="1">ZDC</nxce:center>
            <nxce:center sequenceNumber="2">ZDC</nxce:center>
            <nxce:center sequenceNumber="3">ZID</nxce:center>
            <nxce:sector sequenceNumber="1">ZDCILM</nxce:sector>
            <nxce:airway sequenceNumber="1">Q178</nxce:airway>
          </nxcm:flightTraversalData2>
        </nxcm:ncsmRouteData>
      </fdm:flightPlanInformation>
    </fdm:fltdMessage>
  </fdm:fltdOutput>
</ds:tfmDataService>
""".strip()


def test_parser_extracts_planned_route_and_flight_plan_type() -> None:
    from nfs_ingest.services.swim_parser import parse_tfm_xml_messages

    (record,) = parse_tfm_xml_messages(ROUTE_XML)
    route = record["planned_route"]

    assert record["aircraft_type"] == "C750"
    assert record["equipment_qualifier"] == "L"
    assert [w["seq"] for w in route["waypoints"]] == [1, 2, 3]
    assert route["waypoints"][2] == {
        "seq": 3,
        "lat": 39.2,
        "lon": -78.1,
        "elapsed_s": 600,
    }
    assert route["waypoints"][0]["elapsed_s"] is None
    assert route["fixes"][1] == {"seq": 2, "name": "MCRAY", "elapsed_s": 817}
    assert route["centers"] == ["ZDC", "ZID"]
    assert route["sectors"] == ["ZDCILM"]
    assert route["airways"] == ["Q178"]
    assert route["route_string"].startswith("KIAD.MCRAY2")
    assert route["etd"] == {"type": "PROPOSED", "time": "2026-10-29T03:40:00Z"}
    assert route["departure_procedure"] == "MCRAY2"
    assert route["arrival_procedure"] == "RAZRR5"
    assert route["planned_altitude_ft"] == 43000.0
    assert route["filed_true_airspeed_kts"] == 495.0


def test_route_row_is_line_keyed_by_flight_ref_and_bronze_stays_lean() -> None:
    import json

    batch = prepare_batch([envelope_from_message(FakeMessage(ROUTE_XML), NOW)])

    (route,) = batch.routes
    assert route["flight_ref"] == "143414921"
    assert route["gufi"] == "KW085345tO"
    assert route["waypoint_count"] == 3
    assert json.loads(route["route_geojson"]) == {
        "type": "LineString",
        "coordinates": [[-77.46, 38.95], [-77.8, 39.05], [-78.1, 39.2]],
    }
    assert route["etd_time"] == datetime(2026, 10, 29, 3, 40, tzinfo=UTC)
    assert route["eta_type"] == "ESTIMATED"
    assert batch.stats["planned_route_records"] == 1
    parsed = json.loads(batch.bronze[0]["parsed_records_json"])
    assert "planned_route" not in parsed[0]


def test_message_without_traversal_has_no_route_row() -> None:
    batch = prepare_batch([envelope_from_message(FakeMessage(TRACK_XML), NOW)])

    assert batch.routes == []


def test_zero_zero_position_is_rejected() -> None:
    xml = TRACK_XML.replace('degrees="32" minutes="54"', 'degrees="0" minutes="0"')
    xml = xml.replace('degrees="97"', 'degrees="0"')

    batch = prepare_batch([envelope_from_message(FakeMessage(xml), NOW)])

    assert batch.positions == []


def test_unparseable_payload_is_kept_in_bronze() -> None:
    envelope = envelope_from_message(FakeMessage("<not-xml"), NOW)

    batch = prepare_batch([envelope])

    assert len(batch.bronze) == 1
    assert batch.bronze[0]["parse_error"].startswith("ParseError")
    assert batch.stats["parse_errors"] == 1


def test_duplicate_message_in_batch_is_written_once() -> None:
    envelope = envelope_from_message(FakeMessage(TRACK_XML), NOW)

    batch = prepare_batch([envelope, dict(envelope)])

    assert len(batch.bronze) == 1
    assert batch.stats["duplicate_messages_in_batch"] == 1


def test_flight_key_prefers_gufi_then_flight_ref() -> None:
    assert flight_key({"gufi": "G1", "flight_ref": "F1"}) == "GUFI:G1"
    assert flight_key({"flight_ref": "F1"}) == "FREF:F1"
    assert (
        flight_key({"flight_id": "aal1", "origin": "KDFW", "destination": ""})
        == "ACID:AAL1|KDFW|"
    )


class FakeReceiver:
    def __init__(self, messages: list) -> None:
        self.messages = list(messages)
        self.acked: list = []
        self.terminated = False

    def start(self) -> None:
        return None

    def receive_message(self, timeout: int):
        return self.messages.pop(0) if self.messages else None

    def ack(self, message) -> None:
        self.acked.append(message)

    def terminate(self, grace_period: int) -> None:
        self.terminated = True


class FakeService:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver
        self.disconnected = False

    def connect(self) -> None:
        return None

    def create_persistent_message_receiver_builder(self):
        receiver = self.receiver
        return types.SimpleNamespace(build=lambda queue: receiver)

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def swim_env(monkeypatch):
    monkeypatch.setattr(settings, "swim_host", "tcps://broker.example:55443")
    monkeypatch.setattr(settings, "swim_queue", "queue")
    monkeypatch.setattr(settings, "swim_username", "user")
    monkeypatch.setattr(settings, "swim_password", "password")
    queue_module = types.ModuleType("solace.messaging.resources.queue")
    queue_module.Queue = types.SimpleNamespace(
        durable_exclusive_queue=lambda name: name
    )
    for name in ("solace", "solace.messaging", "solace.messaging.resources"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "solace.messaging.resources.queue", queue_module)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_stream_acks_only_after_each_successful_flush(swim_env) -> None:
    messages = [FakeMessage(TRACK_XML, f"m-{i}") for i in range(5)]
    receiver = FakeReceiver(messages)
    service = FakeService(receiver)
    written: list[int] = []

    def write(envelopes):
        assert len(receiver.acked) == sum(written)
        written.append(len(envelopes))
        return {"positions_written": len(envelopes), "stats": {}}

    result = stream_messages(
        write,
        duration_seconds=40,
        flush_max_messages=2,
        flush_max_seconds=1000,
        service_factory=lambda: service,
        clock=Clock(),
        log=lambda _: None,
    )

    assert written == [2, 2, 1]
    assert receiver.acked == messages
    assert result["acknowledged"] == 5
    assert receiver.terminated and service.disconnected


def test_stream_write_failure_leaves_batch_unacknowledged(swim_env) -> None:
    receiver = FakeReceiver([FakeMessage(TRACK_XML, f"m-{i}") for i in range(3)])
    service = FakeService(receiver)

    def fail(envelopes):
        raise RuntimeError("Delta unavailable")

    with pytest.raises(RuntimeError, match="Delta unavailable"):
        stream_messages(
            fail,
            duration_seconds=40,
            flush_max_messages=2,
            flush_max_seconds=1000,
            service_factory=lambda: service,
            clock=Clock(),
            log=lambda _: None,
        )

    assert receiver.acked == []
    assert receiver.terminated and service.disconnected


def test_spark_schemas_are_optional_without_pyspark() -> None:
    # Importing the module must not require pyspark (app and unit tests).
    assert hasattr(swim_pipeline, "_BRONZE_SCHEMA")
