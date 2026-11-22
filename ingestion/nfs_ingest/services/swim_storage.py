"""Databricks SQL Bronze writer used by the R14 end-to-end test."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from nfs_ingest.config import settings

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _table_identifier(table_name: str) -> str:
    parts = table_name.split(".")
    if len(parts) != 3 or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("SWIM Bronze table must be a three-part Unity Catalog name.")
    return ".".join(f"`{part}`" for part in parts)


class SwimBronzeWriter:
    """Create, insert, and verify raw SWIM messages through a SQL warehouse."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        warehouse_id: str | None = None,
        table_name: str | None = None,
        profile: str | None = None,
    ) -> None:
        selected_profile = profile or settings.databricks_profile or None
        self.client = client or (
            WorkspaceClient(profile=selected_profile)
            if selected_profile
            else WorkspaceClient()
        )
        self.warehouse_id = warehouse_id or settings.databricks_warehouse_id
        if not self.warehouse_id:
            raise ValueError("DATABRICKS_WAREHOUSE_ID is required.")
        self.table_name = table_name or settings.swim_bronze_table
        self.table_identifier = _table_identifier(self.table_name)

    def ensure_table(self) -> None:
        catalog, schema, _ = self.table_name.split(".")
        self._execute(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`")
        self._execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_identifier} (
              event_id STRING NOT NULL,
              received_at TIMESTAMP NOT NULL,
              topic STRING,
              application_message_id STRING,
              payload_sha256 STRING NOT NULL,
              payload_xml STRING NOT NULL,
              properties_json STRING,
              parsed_records_json STRING,
              parsed_record_count INT NOT NULL,
              source STRING NOT NULL
            ) USING DELTA
            """
        )

    def write(self, envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope["payload_xml"]
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        application_message_id = envelope.get("application_message_id") or ""
        event_id = hashlib.sha256(
            f"{application_message_id}:{payload_hash}".encode("utf-8")
        ).hexdigest()
        parameters = [
            StatementParameterListItem(name="event_id", value=event_id, type="STRING"),
            StatementParameterListItem(
                name="received_at",
                value=envelope["received_at"],
                type="TIMESTAMP",
            ),
            StatementParameterListItem(
                name="topic", value=envelope.get("topic") or "", type="STRING"
            ),
            StatementParameterListItem(
                name="application_message_id",
                value=application_message_id,
                type="STRING",
            ),
            StatementParameterListItem(
                name="payload_sha256", value=payload_hash, type="STRING"
            ),
            StatementParameterListItem(
                name="payload_xml", value=payload, type="STRING"
            ),
            StatementParameterListItem(
                name="properties_json",
                value=json.dumps(envelope.get("properties") or {}, default=str),
                type="STRING",
            ),
            StatementParameterListItem(
                name="parsed_records_json",
                value=json.dumps(envelope.get("parsed_records") or [], default=str),
                type="STRING",
            ),
            StatementParameterListItem(
                name="parsed_record_count",
                value=str(len(envelope.get("parsed_records") or [])),
                type="INT",
            ),
            StatementParameterListItem(
                name="source", value="FAA_SWIM_TFM_R14", type="STRING"
            ),
        ]
        self._execute(
            f"""
            INSERT INTO {self.table_identifier} VALUES (
              :event_id,
              :received_at,
              :topic,
              :application_message_id,
              :payload_sha256,
              :payload_xml,
              :properties_json,
              :parsed_records_json,
              :parsed_record_count,
              :source
            )
            """,
            parameters=parameters,
        )
        verification = self._execute(
            f"""
            SELECT event_id, received_at, topic, parsed_record_count
            FROM {self.table_identifier}
            WHERE event_id = :event_id
            ORDER BY received_at DESC
            LIMIT 1
            """,
            parameters=[
                StatementParameterListItem(
                    name="event_id", value=event_id, type="STRING"
                )
            ],
        )
        rows = verification.result.data_array if verification.result else None
        if not rows:
            raise RuntimeError(
                "Databricks insert completed but verification found no row."
            )
        return {
            "event_id": event_id,
            "payload_sha256": payload_hash,
            "table": self.table_name,
            "verified": True,
            "parsed_record_count": len(envelope.get("parsed_records") or []),
        }

    def _execute(
        self,
        statement: str,
        *,
        parameters: list[StatementParameterListItem] | None = None,
    ) -> Any:
        response = self.client.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=self.warehouse_id,
            parameters=parameters,
            wait_timeout="50s",
        )
        while response.status.state in {
            StatementState.PENDING,
            StatementState.RUNNING,
        }:
            response = self.client.statement_execution.get_statement(
                response.statement_id
            )
        if response.status.state != StatementState.SUCCEEDED:
            detail = (
                response.status.error.message if response.status.error else "unknown"
            )
            raise RuntimeError(f"Databricks SQL statement failed: {detail}")
        return response


def build_envelope(message: Any, payload_xml: str, parsed_records: list[dict]) -> dict:
    """Convert an inbound Solace message to the stable Bronze record contract."""
    return {
        "received_at": datetime.now(UTC).isoformat(),
        "topic": message.get_destination_name(),
        "application_message_id": message.get_application_message_id(),
        "properties": message.get_properties(),
        "payload_xml": payload_xml,
        "parsed_records": parsed_records,
    }
