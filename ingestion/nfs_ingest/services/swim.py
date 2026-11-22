"""FAA SWIM R14 Solace broker connectivity helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nfs_ingest.config import settings
from nfs_ingest.services.swim_parser import parse_tfm_xml_messages
from nfs_ingest.services.swim_storage import build_envelope


def _configured() -> bool:
    return bool(
        settings.swim_host
        and settings.swim_vpn
        and settings.swim_queue
        and settings.swim_username
        and settings.swim_password
    )


def _redact_error(error: Exception) -> str:
    message = str(error)
    for secret in (settings.swim_username, settings.swim_password):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


def _build_service() -> Any:
    from requests.certs import where
    from solace.messaging.config.retry_strategy import RetryStrategy
    from solace.messaging.config.transport_security_strategy import TLS
    from solace.messaging.messaging_service import MessagingService

    if settings.swim_tls_validate:
        bundled_trust_store = Path(__file__).resolve().parents[1] / "certs"
        trust_store = settings.swim_cert_path or str(
            bundled_trust_store
            if bundled_trust_store.exists()
            else Path(where()).parent
        )
        security = TLS.create().with_certificate_validation(
            False,
            True,
            trust_store,
        )
    else:
        security = TLS.create().without_certificate_validation()

    retry = RetryStrategy.parametrized_retry(
        settings.swim_connect_retries,
        settings.swim_retry_interval_ms,
    )
    return (
        MessagingService.builder()
        .from_properties(
            {
                "solace.messaging.transport.host": settings.swim_host,
                "solace.messaging.service.vpn-name": settings.swim_vpn,
                "solace.messaging.authentication.scheme.basic.username": (
                    settings.swim_username
                ),
                "solace.messaging.authentication.scheme.basic.password": (
                    settings.swim_password
                ),
            }
        )
        .with_connection_retry_strategy(retry)
        .with_reconnection_retry_strategy(retry)
        .with_transport_security_strategy(security)
        .build()
    )


def probe_connection(service_factory: Any | None = None) -> dict[str, Any]:
    """Connect to SWIM and bind the configured queue without consuming data."""
    checked_at = datetime.now(UTC)
    if not _configured():
        return {
            "provider": "faa-swim-r14",
            "state": "configuration-error",
            "connected": False,
            "checked_at": checked_at.isoformat(),
            "detail": "Missing SWIM queue credentials.",
        }

    service = None
    receiver = None
    try:
        from solace.messaging.resources.queue import Queue

        service = (service_factory or _build_service)()
        service.connect()
        queue = Queue.durable_exclusive_queue(settings.swim_queue)
        receiver = service.create_persistent_message_receiver_builder().build(queue)
        receiver.start()
        return {
            "provider": "faa-swim-r14",
            "state": "connected",
            "connected": True,
            "checked_at": checked_at.isoformat(),
            "queue_bound": True,
            "tls_validation": settings.swim_tls_validate,
        }
    except Exception as exc:
        return {
            "provider": "faa-swim-r14",
            "state": "connection-error",
            "connected": False,
            "checked_at": checked_at.isoformat(),
            "detail": _redact_error(exc),
        }
    finally:
        if receiver is not None:
            try:
                receiver.terminate(0)
            except Exception:
                pass
        if service is not None:
            try:
                service.disconnect()
            except Exception:
                pass


def consume_one_message(
    processor: Any,
    *,
    timeout_ms: int | None = None,
    service_factory: Any | None = None,
) -> dict[str, Any]:
    """Receive one message and acknowledge it only after processing succeeds."""
    if not _configured():
        raise ValueError("Missing SWIM queue credentials.")

    from solace.messaging.resources.queue import Queue

    service = (service_factory or _build_service)()
    receiver = None
    try:
        service.connect()
        queue = Queue.durable_exclusive_queue(settings.swim_queue)
        receiver = service.create_persistent_message_receiver_builder().build(queue)
        receiver.start()
        message = receiver.receive_message(
            timeout_ms if timeout_ms is not None else settings.swim_receive_timeout_ms
        )
        if message is None:
            return {
                "state": "timeout",
                "received": False,
                "acknowledged": False,
            }
        payload_xml = message.get_payload_as_string()
        if not payload_xml:
            raise ValueError("SWIM message did not contain a string payload.")
        parsed_records = parse_tfm_xml_messages(payload_xml)
        result = processor(build_envelope(message, payload_xml, parsed_records))
        receiver.ack(message)
        return {
            "state": "stored",
            "received": True,
            "acknowledged": True,
            "topic": message.get_destination_name(),
            "payload_bytes": len(payload_xml.encode("utf-8")),
            "parsed_record_count": len(parsed_records),
            "storage": result,
        }
    finally:
        if receiver is not None:
            try:
                receiver.terminate(0)
            except Exception:
                pass
        try:
            service.disconnect()
        except Exception:
            pass

