"""Tests for SWIM parsing and receive/write/ack ordering."""

from __future__ import annotations

import pytest

from nfs_ingest.config import settings
from nfs_ingest.services.swim import consume_one_message
from nfs_ingest.services.swim_parser import parse_tfm_xml_messages

SAMPLE_XML = """
<tfmDataService>
  <fltdMessage acid="AAL123" depArpt="KDFW" arrArpt="KJFK"
      sourceTimeStamp="2026-10-27T12:00:00Z">
    <gufi>GUFI-123</gufi>
    <position>
      <latitudeDecimal>32.9</latitudeDecimal>
      <longitudeDecimal>-97.0</longitudeDecimal>
      <simpleAltitude>350</simpleAltitude>
      <groundSpeed>420</groundSpeed>
    </position>
  </fltdMessage>
</tfmDataService>
""".strip()


class FakeMessage:
    def get_payload_as_string(self) -> str:
        return SAMPLE_XML

    def get_destination_name(self) -> str:
        return "tfm/r14"

    def get_application_message_id(self) -> str:
        return "message-1"

    def get_properties(self) -> dict:
        return {"sequence": 1}


class FakeReceiver:
    def __init__(self) -> None:
        self.message = FakeMessage()
        self.acknowledged = False
        self.terminated = False

    def start(self) -> None:
        return None

    def receive_message(self, timeout: int) -> FakeMessage:
        return self.message

    def ack(self, message: FakeMessage) -> None:
        assert message is self.message
        self.acknowledged = True

    def terminate(self, grace_period: int) -> None:
        self.terminated = True


class FakeBuilder:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver

    def build(self, queue) -> FakeReceiver:
        return self.receiver


class FakeService:
    def __init__(self) -> None:
        self.receiver = FakeReceiver()
        self.disconnected = False

    def connect(self) -> None:
        return None

    def create_persistent_message_receiver_builder(self) -> FakeBuilder:
        return FakeBuilder(self.receiver)

    def disconnect(self) -> None:
        self.disconnected = True


def _configure_swim(monkeypatch) -> None:
    monkeypatch.setattr(settings, "swim_host", "tcps://broker.example:55443")
    monkeypatch.setattr(settings, "swim_queue", "queue")
    monkeypatch.setattr(settings, "swim_username", "user")
    monkeypatch.setattr(settings, "swim_password", "password")


def test_parser_extracts_flight_record() -> None:
    records = parse_tfm_xml_messages(SAMPLE_XML)

    assert records == [
        {
            "flight_id": "AAL123",
            "gufi": "GUFI-123",
            "origin": "KDFW",
            "destination": "KJFK",
            "current_lat": 32.9,
            "current_lon": -97.0,
            "current_altitude_ft": 35000.0,
            "velocity_kts": 420.0,
            "msg_type": "",
            "flight_ref": "",
            "aircraft_type": "",
            "aircraft_engine_class": "",
            "equipment_qualifier": "",
            "special_aircraft_qualifier": "",
            "aircraft_category": "",
            "user_category": "",
            "planned_route": None,
            "event_time": "2026-10-27T12:00:00Z",
            "source": "FAA_SWIM_TFM_R14",
        }
    ]


def test_parser_uses_null_for_missing_coordinates() -> None:
    records = parse_tfm_xml_messages(
        """
        <tfmDataService>
          <fltdMessage acid="UAL2231" sourceTimeStamp="2026-10-27T12:51:29Z">
            <simpleAltitude>370</simpleAltitude>
          </fltdMessage>
        </tfmDataService>
        """
    )

    assert records[0]["current_lat"] is None
    assert records[0]["current_lon"] is None


def test_message_is_acknowledged_only_after_storage(monkeypatch) -> None:
    _configure_swim(monkeypatch)
    service = FakeService()

    def store(envelope: dict) -> dict:
        assert service.receiver.acknowledged is False
        assert envelope["parsed_records"][0]["flight_id"] == "AAL123"
        return {"verified": True}

    result = consume_one_message(store, service_factory=lambda: service)

    assert result["acknowledged"] is True
    assert service.receiver.acknowledged is True
    assert service.receiver.terminated is True
    assert service.disconnected is True


def test_storage_failure_leaves_message_unacknowledged(monkeypatch) -> None:
    _configure_swim(monkeypatch)
    service = FakeService()

    def fail_storage(envelope: dict) -> dict:
        raise RuntimeError("Databricks unavailable")

    with pytest.raises(RuntimeError, match="Databricks unavailable"):
        consume_one_message(fail_storage, service_factory=lambda: service)

    assert service.receiver.acknowledged is False
    assert service.receiver.terminated is True
    assert service.disconnected is True
