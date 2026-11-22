"""Batched Databricks SQL persistence for normalized NMS records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import (
    StatementParameterListItem,
    StatementState,
)

from nfs_ingest.config import settings
from nfs_ingest.services.nms_parser import PreparedNmsRecord
from nfs_ingest.services.nms_state import (
    _qualified_identifier,
    _string_literal,
    _timestamp_literal,
)
from nfs_ingest.services.nms_watermark import NmsSyncPlan


class NmsDatabricksWriter:
    """Idempotently persist one NMS delta into Bronze and Silver tables."""

    def __init__(
        self,
        *,
        warehouse_id: str | None = None,
        profile: str | None = None,
        client: WorkspaceClient | None = None,
        bronze_table: str | None = None,
        versions_table: str | None = None,
        hazards_table: str | None = None,
    ) -> None:
        selected_warehouse = warehouse_id or settings.databricks_warehouse_id
        if not selected_warehouse:
            raise ValueError("A Databricks SQL warehouse ID is required.")
        selected_profile = profile or settings.databricks_profile or None
        self.warehouse_id = selected_warehouse
        self.client = client or WorkspaceClient(profile=selected_profile)
        self.bronze_table = _qualified_identifier(
            bronze_table or settings.nms_bronze_table
        )
        self.versions_table = _qualified_identifier(
            versions_table or settings.notam_versions_table
        )
        self.hazards_table = _qualified_identifier(
            hazards_table or settings.notam_hazards_table
        )

    def write(self, records: list[PreparedNmsRecord]) -> dict[str, Any]:
        if not records:
            return {
                "bronze_records": 0,
                "notam_versions": 0,
                "hazard_volumes": 0,
                "verified": True,
            }
        bronze_rows = [record.bronze for record in records]
        version_rows = [record.version for record in records]
        hazard_rows = [record.hazard for record in records if record.hazard]
        self._merge_bronze(bronze_rows)
        self._close_previous_versions(version_rows)
        self._merge_versions(version_rows)
        if hazard_rows:
            self._merge_hazards(hazard_rows)
        bronze_count = self._verify_ids(
            self.bronze_table,
            "event_id",
            [row["event_id"] for row in bronze_rows],
        )
        version_count = self._verify_ids(
            self.versions_table,
            "notam_version_id",
            [row["notam_version_id"] for row in version_rows],
        )
        hazard_count = self._verify_ids(
            self.hazards_table,
            "hazard_id",
            [row["hazard_id"] for row in hazard_rows],
        )
        if bronze_count != len(bronze_rows) or version_count != len(version_rows):
            raise RuntimeError(
                "NMS persistence verification count did not match input."
            )
        if hazard_count != len(hazard_rows):
            raise RuntimeError(
                "NMS hazard persistence verification count did not match input."
            )
        return {
            "bronze_records": bronze_count,
            "notam_versions": version_count,
            "hazard_volumes": hazard_count,
            "verified": True,
        }

    def _merge_bronze(self, rows: list[dict[str, Any]]) -> None:
        self._execute(
            f"""
            MERGE INTO {self.bronze_table} AS target
            USING (
              SELECT
                row.event_id,
                row.nms_id,
                try_cast(row.fetched_at AS TIMESTAMP) AS fetched_at,
                try_cast(row.last_updated_at AS TIMESTAMP) AS last_updated_at,
                row.request_mode,
                try_cast(row.request_watermark AS TIMESTAMP) AS request_watermark,
                row.payload_sha256,
                row.payload_json,
                row.source
              FROM explode(from_json(:rows_json, '{_BRONZE_SCHEMA}')) AS source(row)
            ) AS source
            ON target.event_id = source.event_id
            WHEN NOT MATCHED THEN INSERT *
            """,
            rows,
        )

    def _close_previous_versions(self, rows: list[dict[str, Any]]) -> None:
        self._execute(
            f"""
            MERGE INTO {self.versions_table} AS target
            USING (
              SELECT
                row.notam_version_id,
                row.nms_id,
                try_cast(row.valid_from AS TIMESTAMP) AS valid_from
              FROM explode(from_json(:rows_json, '{_VERSION_SCHEMA}')) AS source(row)
            ) AS source
            ON target.nms_id = source.nms_id AND target.is_current = true
            WHEN MATCHED AND target.notam_version_id <> source.notam_version_id
              THEN UPDATE SET
                target.is_current = false,
                target.valid_to = source.valid_from
            """,
            rows,
        )

    def _merge_versions(self, rows: list[dict[str, Any]]) -> None:
        self._execute(
            f"""
            MERGE INTO {self.versions_table} AS target
            USING (
              SELECT
                row.notam_version_id,
                row.bronze_event_id,
                row.nms_id,
                row.notam_id,
                row.notam_type,
                row.status,
                row.location,
                row.replaces_notam_version_id,
                try_cast(row.effective_start AS TIMESTAMP) AS effective_start,
                try_cast(row.effective_end AS TIMESTAMP) AS effective_end,
                row.raw_text,
                row.category,
                row.phase_of_flight,
                try_cast(row.valid_from AS TIMESTAMP) AS valid_from,
                try_cast(row.valid_to AS TIMESTAMP) AS valid_to,
                row.is_current,
                try_cast(row.parsed_at AS TIMESTAMP) AS parsed_at,
                row.parser_version,
                row.source,
                row.anchor_longitude,
                row.anchor_latitude,
                CASE
                  WHEN row.anchor_longitude IS NOT NULL
                    AND row.anchor_latitude IS NOT NULL
                  THEN h3_longlatash3(row.anchor_longitude, row.anchor_latitude, 7)
                END AS anchor_h3_r7,
                row.anchor_source,
                row.anchor_radius_nm,
                row.anchor_qline_offset_nm
              FROM explode(from_json(:rows_json, '{_VERSION_SCHEMA}')) AS source(row)
            ) AS source
            ON target.notam_version_id = source.notam_version_id
            WHEN NOT MATCHED THEN INSERT *
            """,
            rows,
        )

    def _merge_hazards(self, rows: list[dict[str, Any]]) -> None:
        self._execute(
            f"""
            MERGE INTO {self.hazards_table} AS target
            USING (
              SELECT
                row.hazard_id,
                row.notam_version_id,
                row.nms_id,
                row.notam_id,
                row.location,
                try_cast(row.effective_start AS TIMESTAMP) AS effective_start,
                try_cast(row.effective_end AS TIMESTAMP) AS effective_end,
                row.geometry_geojson,
                row.geometry_source,
                row.lower_altitude_ft,
                row.upper_altitude_ft,
                row.lower_altitude_reference,
                row.upper_altitude_reference,
                row.horizontal_radius_nm,
                row.circle_center_longitude,
                row.circle_center_latitude,
                row.horizontal_buffer_nm,
                row.h3_resolution,
                coalesce(
                  h3_try_coverash3(row.geometry_geojson, row.h3_resolution),
                  cast(array() AS ARRAY<BIGINT>)
                ) AS h3_cells,
                row.parse_confidence,
                row.parse_warnings,
                row.parser_version,
                row.source,
                try_cast(row.created_at AS TIMESTAMP) AS created_at
              FROM explode(from_json(:rows_json, '{_HAZARD_SCHEMA}')) AS source(row)
            ) AS source
            ON target.hazard_id = source.hazard_id
            WHEN NOT MATCHED THEN INSERT *
            """,
            rows,
        )

    def _verify_ids(
        self,
        table: str,
        id_column: str,
        identifiers: list[str],
    ) -> int:
        if not identifiers:
            return 0
        response = self._execute(
            f"""
            SELECT count(*) AS matched
            FROM {table} AS target
            JOIN (
              SELECT explode(from_json(:ids_json, 'ARRAY<STRING>')) AS id
            ) AS expected
              ON target.{id_column} = expected.id
            """,
            parameters=[
                StatementParameterListItem(
                    name="ids_json",
                    value=json.dumps(identifiers),
                    type="STRING",
                )
            ],
        )
        rows = response.result.data_array if response.result else []
        return int(rows[0][0]) if rows else 0

    def _execute(
        self,
        statement: str,
        rows: list[dict[str, Any]] | None = None,
        *,
        parameters: list[StatementParameterListItem] | None = None,
    ) -> Any:
        if rows is not None:
            parameters = [
                StatementParameterListItem(
                    name="rows_json",
                    value=json.dumps(rows, default=str, separators=(",", ":")),
                    type="STRING",
                )
            ]
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
                response.status.error.message
                if response.status.error
                else "unknown"
            )
            raise RuntimeError(f"Databricks SQL statement failed: {detail}")
        return response


class NmsSqlWatermarkStore:
    """Manage the NMS operational watermark through a SQL warehouse."""

    def __init__(
        self,
        *,
        warehouse_id: str | None = None,
        profile: str | None = None,
        client: WorkspaceClient | None = None,
        table_name: str | None = None,
    ) -> None:
        selected_warehouse = warehouse_id or settings.databricks_warehouse_id
        if not selected_warehouse:
            raise ValueError("A Databricks SQL warehouse ID is required.")
        selected_profile = profile or settings.databricks_profile or None
        self.warehouse_id = selected_warehouse
        self.client = client or WorkspaceClient(profile=selected_profile)
        self.table_name = table_name or settings.nms_watermark_table
        self.table_identifier = _qualified_identifier(self.table_name)

    def ensure_table(self) -> None:
        catalog, schema, _ = self.table_name.split(".")
        self._execute(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`")
        self._execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table_identifier} (
              feed_name STRING NOT NULL,
              last_successful_pull_at TIMESTAMP,
              last_run_started_at TIMESTAMP,
              last_run_completed_at TIMESTAMP,
              last_run_mode STRING,
              last_run_status STRING,
              last_request_id STRING,
              records_written BIGINT,
              error_detail STRING,
              updated_at TIMESTAMP NOT NULL
            ) USING DELTA
            """
        )

    def get_last_successful_pull_at(
        self,
        feed_name: str = "faa-nms-notams",
    ) -> datetime | None:
        response = self._execute(
            f"""
            SELECT last_successful_pull_at
            FROM {self.table_identifier}
            WHERE feed_name = {_string_literal(feed_name)}
            LIMIT 1
            """
        )
        rows = response.result.data_array if response.result else []
        if not rows or rows[0][0] is None:
            return None
        value = datetime.fromisoformat(str(rows[0][0]).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def mark_started(
        self,
        plan: NmsSyncPlan,
        feed_name: str = "faa-nms-notams",
    ) -> None:
        self._merge_status(
            feed_name=feed_name,
            plan=plan,
            status="running",
            completed_at=None,
            request_id=None,
            records_written=None,
            error_detail=None,
            advance_watermark=False,
        )

    def mark_succeeded(
        self,
        plan: NmsSyncPlan,
        *,
        request_id: str | None,
        records_written: int,
        completed_at: datetime | None = None,
        feed_name: str = "faa-nms-notams",
        advance_watermark: bool = True,
    ) -> None:
        self._merge_status(
            feed_name=feed_name,
            plan=plan,
            status="succeeded" if advance_watermark else "shadow-succeeded",
            completed_at=completed_at or datetime.now(UTC),
            request_id=request_id,
            records_written=records_written,
            error_detail=None,
            advance_watermark=advance_watermark,
        )

    def mark_failed(
        self,
        plan: NmsSyncPlan,
        *,
        error_detail: str,
        completed_at: datetime | None = None,
        feed_name: str = "faa-nms-notams",
    ) -> None:
        self._merge_status(
            feed_name=feed_name,
            plan=plan,
            status="failed",
            completed_at=completed_at or datetime.now(UTC),
            request_id=None,
            records_written=None,
            error_detail=error_detail[:2000],
            advance_watermark=False,
        )

    def _merge_status(
        self,
        *,
        feed_name: str,
        plan: NmsSyncPlan,
        status: str,
        completed_at: datetime | None,
        request_id: str | None,
        records_written: int | None,
        error_detail: str | None,
        advance_watermark: bool,
    ) -> None:
        watermark = (
            _timestamp_literal(plan.commit_watermark_at)
            if advance_watermark
            else "target.last_successful_pull_at"
        )
        inserted_watermark = (
            _timestamp_literal(plan.commit_watermark_at)
            if advance_watermark
            else "NULL"
        )
        records = "NULL" if records_written is None else str(records_written)
        now = _timestamp_literal(datetime.now(UTC))
        self._execute(
            f"""
            MERGE INTO {self.table_identifier} AS target
            USING (SELECT {_string_literal(feed_name)} AS feed_name) AS source
            ON target.feed_name = source.feed_name
            WHEN MATCHED THEN UPDATE SET
              target.last_successful_pull_at = {watermark},
              target.last_run_started_at = {_timestamp_literal(plan.run_started_at)},
              target.last_run_completed_at = {_timestamp_literal(completed_at)},
              target.last_run_mode = {_string_literal(plan.mode)},
              target.last_run_status = {_string_literal(status)},
              target.last_request_id = {_string_literal(request_id)},
              target.records_written = {records},
              target.error_detail = {_string_literal(error_detail)},
              target.updated_at = {now}
            WHEN NOT MATCHED THEN INSERT (
              feed_name, last_successful_pull_at, last_run_started_at,
              last_run_completed_at, last_run_mode, last_run_status,
              last_request_id, records_written, error_detail, updated_at
            ) VALUES (
              {_string_literal(feed_name)}, {inserted_watermark},
              {_timestamp_literal(plan.run_started_at)},
              {_timestamp_literal(completed_at)}, {_string_literal(plan.mode)},
              {_string_literal(status)}, {_string_literal(request_id)},
              {records}, {_string_literal(error_detail)}, {now}
            )
            """
        )

    def _execute(self, statement: str) -> Any:
        response = self.client.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=self.warehouse_id,
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
                response.status.error.message
                if response.status.error
                else "unknown"
            )
            raise RuntimeError(f"Databricks SQL statement failed: {detail}")
        return response


_BRONZE_SCHEMA = (
    "ARRAY<STRUCT<event_id:STRING,nms_id:STRING,fetched_at:STRING,"
    "last_updated_at:STRING,request_mode:STRING,request_watermark:STRING,"
    "payload_sha256:STRING,payload_json:STRING,source:STRING>>"
)
_VERSION_SCHEMA = (
    "ARRAY<STRUCT<notam_version_id:STRING,bronze_event_id:STRING,nms_id:STRING,"
    "notam_id:STRING,notam_type:STRING,status:STRING,location:STRING,"
    "replaces_notam_version_id:STRING,effective_start:STRING,effective_end:STRING,"
    "raw_text:STRING,category:STRING,phase_of_flight:ARRAY<STRING>,"
    "valid_from:STRING,valid_to:STRING,is_current:BOOLEAN,parsed_at:STRING,"
    "parser_version:STRING,source:STRING,anchor_longitude:DOUBLE,"
    "anchor_latitude:DOUBLE,anchor_source:STRING,anchor_radius_nm:DOUBLE,"
    "anchor_qline_offset_nm:DOUBLE>>"
)
_HAZARD_SCHEMA = (
    "ARRAY<STRUCT<hazard_id:STRING,notam_version_id:STRING,nms_id:STRING,"
    "notam_id:STRING,location:STRING,effective_start:STRING,effective_end:STRING,"
    "geometry_geojson:STRING,geometry_source:STRING,lower_altitude_ft:DOUBLE,"
    "upper_altitude_ft:DOUBLE,lower_altitude_reference:STRING,"
    "upper_altitude_reference:STRING,horizontal_radius_nm:DOUBLE,"
    "circle_center_longitude:DOUBLE,circle_center_latitude:DOUBLE,"
    "horizontal_buffer_nm:DOUBLE,h3_resolution:INT,parse_confidence:DOUBLE,"
    "parse_warnings:ARRAY<STRING>,parser_version:STRING,source:STRING,"
    "created_at:STRING>>"
)

