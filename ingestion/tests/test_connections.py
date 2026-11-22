"""Unit tests for provider connection probes."""

from nfs_ingest.config import settings
from nfs_ingest.services.nms import NmsClient
from nfs_ingest.services.nms import probe_connection as probe_nms
from nfs_ingest.services.swim import probe_connection as probe_swim


class FakeResponse:
    def __init__(self, payload: dict, headers: dict | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeNmsSession:
    def post(self, *args, **kwargs) -> FakeResponse:
        return FakeResponse({"access_token": "token"})

    def get(self, *args, **kwargs) -> FakeResponse:
        return FakeResponse(
            {"status": "Success", "data": {"geojson": [{"id": "one"}]}},
            {"x-request-id": "request-123"},
        )


class FakeReceiver:
    def __init__(self) -> None:
        self.started = False
        self.terminated = False

    def start(self) -> None:
        self.started = True

    def terminate(self, grace_period: int) -> None:
        self.terminated = True


class FakeReceiverBuilder:
    def __init__(self, receiver: FakeReceiver) -> None:
        self.receiver = receiver

    def build(self, queue) -> FakeReceiver:
        return self.receiver


class FakeSwimService:
    def __init__(self) -> None:
        self.receiver = FakeReceiver()
        self.connected = False
        self.disconnected = False

    def connect(self) -> None:
        self.connected = True

    def create_persistent_message_receiver_builder(self) -> FakeReceiverBuilder:
        return FakeReceiverBuilder(self.receiver)

    def disconnect(self) -> None:
        self.disconnected = True


def test_nms_probe_authenticates_and_reads_delta(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nms_client_id", "client")
    monkeypatch.setattr(settings, "nms_client_secret", "secret")

    result = probe_nms(NmsClient(FakeNmsSession()))

    assert result["connected"] is True
    assert result["records_returned"] == 1
    assert result["request_id"] == "request-123"


def test_nms_probe_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nms_client_id", "")
    monkeypatch.setattr(settings, "nms_client_secret", "")

    result = probe_nms()

    assert result["connected"] is False
    assert result["state"] == "configuration-error"


def test_swim_probe_connects_and_binds_queue(monkeypatch) -> None:
    monkeypatch.setattr(settings, "swim_host", "tcps://broker.example:55443")
    monkeypatch.setattr(settings, "swim_queue", "queue")
    monkeypatch.setattr(settings, "swim_username", "user")
    monkeypatch.setattr(settings, "swim_password", "password")
    service = FakeSwimService()

    result = probe_swim(lambda: service)

    assert result["connected"] is True
    assert result["queue_bound"] is True
    assert service.connected is True
    assert service.receiver.started is True
    assert service.receiver.terminated is True
    assert service.disconnected is True


def test_swim_probe_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.setattr(settings, "swim_queue", "")
    monkeypatch.setattr(settings, "swim_username", "")
    monkeypatch.setattr(settings, "swim_password", "")

    result = probe_swim()

    assert result["connected"] is False
    assert result["state"] == "configuration-error"
