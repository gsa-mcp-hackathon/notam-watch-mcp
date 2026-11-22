"""Ingestion job settings: the MCP server's settings (Lakebase, .env
precedence) plus feed endpoints, credentials, Delta table names and retention.

Precedence is the same as ``server.config``: ``Settings(...)`` > .env >
environment variables > defaults. Credentials still unset after that are
filled from the Databricks secret scope by ``apply_secret_scope()``, which
the job entry points call. Off Databricks, put the credentials in .env: the
secret scope (and the Databricks SDK) is then never contacted.
"""

import base64
import logging

from server.config import Settings as ServerSettings
from server.config import env_files

logger = logging.getLogger(__name__)

__all__ = ["Settings", "env_files", "settings"]

# Settings field -> key in the Databricks secret scope (settings.nfs_secret_scope).
_SECRET_KEYS = {
    "nms_auth_url": "nms-auth-url",
    "nms_api_base_url": "nms-base-url",
    "nms_client_id": "nms-client-id",
    "nms_client_secret": "nms-client-secret",
    "swim_host": "swim-host",
    "swim_username": "swim-username",
    "swim_password": "swim-password",
    "swim_queue": "swim-queue",
}


class Settings(ServerSettings):
    """Settings for the NMS, SWIM, NASR/SAA, gold publish and retention jobs."""

    nms_auth_url: str = ""
    nms_api_base_url: str = ""
    nms_client_id: str = ""
    nms_client_secret: str = ""
    nms_timeout_seconds: int = 30
    nms_probe_window_minutes: int = 1
    # Classification baselines loaded by `--mode initial` (comma separated).
    # Valid: INTERNATIONAL, MILITARY, LOCAL_MILITARY, DOMESTIC, FDC.
    # FDC carries TFRs, special-use airspace activations and other en-route
    # / procedural NOTAMs; it is part of the default baseline. Pass
    # --classifications to the initial-load job to load a subset.
    nms_baseline_classifications: str = "DOMESTIC,FDC"
    nms_content_timeout_seconds: int = 300
    # Records per Databricks SQL MERGE batch (keeps statement payloads small).
    nms_write_batch_size: int = 1000
    nms_incremental_max_lookback_hours: int = 24
    nms_incremental_overlap_minutes: int = 2
    nms_watermark_table: str = (
        "users.nfs_demo.nfs_ingestion_watermarks"
    )
    nms_bronze_table: str = (
        "users.nfs_demo.nfs_nms_notam_events_raw"
    )
    notam_versions_table: str = (
        "users.nfs_demo.nfs_notam_versions"
    )
    notam_hazards_table: str = (
        "users.nfs_demo.nfs_notam_hazard_volumes"
    )
    # Horizontal modeling buffer for obstacle (OBST) point NOTAM cylinders.
    # This is a safety/modeling assumption, not a source radius.
    obstacle_buffer_nm: float = 0.5

    # FAA NASR 28-day subscription (public). APT CSV files are published as
    # <base>/extra/DD_Mon_YYYY_APT_CSV.zip for each AIRAC effective date.
    nasr_base_url: str = "https://nfdc.faa.gov/webContent/28DaySub"
    nasr_timeout_seconds: int = 120
    nasr_airports_table: str = "users.nfs_demo.nfs_airports"
    # FAA Special Use Airspace shapes (SAA AIXM from the same subscription).
    sua_airspaces_table: str = "users.nfs_demo.nfs_sua_airspaces"
    # FAA Aircraft Characteristics Database
    # (ingestion/resources/sql/aircraft_reference.sql).
    aircraft_reference_table: str = (
        "users.nfs_demo.nfs_aircraft_reference"
    )

    swim_host: str = ""
    swim_vpn: str = "TFMS"
    swim_queue: str = ""
    swim_username: str = ""
    swim_password: str = ""
    swim_cert_path: str = ""
    swim_tls_validate: bool = True
    swim_connect_retries: int = 1
    swim_retry_interval_ms: int = 1000
    swim_receive_timeout_ms: int = 120000
    swim_bronze_table: str = (
        "users.nfs_demo.nfs_swim_flight_events_raw"
    )
    swim_positions_table: str = (
        "users.nfs_demo.nfs_flight_position_events"
    )
    swim_flights_current_table: str = (
        "users.nfs_demo.nfs_flights_current"
    )
    # Latest aircraft type/categories per TFMS flightRef (joins to positions
    # and flights_current on flight_ref).
    swim_flight_aircraft_table: str = (
        "users.nfs_demo.nfs_flight_aircraft"
    )
    # Latest TFMS planned route per flightRef (flightTraversalData2).
    swim_planned_routes_table: str = (
        "users.nfs_demo.nfs_flight_planned_routes"
    )
    # Streaming job: run length per job run (continuous jobs restart it),
    # and micro-batch flush thresholds. Messages are acked after each flush.
    swim_stream_duration_seconds: int = 1740
    swim_flush_max_messages: int = 500
    swim_flush_max_seconds: int = 30

    # SWIM retention (nfs_ingest/services/retention.py). Cutoffs are relative
    # to each table's newest timestamp, so a paused feed never wipes data.
    swim_bronze_retention_hours: float = 12
    swim_positions_retention_hours: float = 26
    flights_current_stale_hours: float = 2
    # When True, bronze/positions only delete rows already covered by the gold
    # analytics watermark (nfs_ingestion_watermarks.feed_name below).
    retention_require_gold_watermark: bool = False
    gold_watermark_feed: str = "gold-flight-analytics"
    retention_audit_table: str = "users.nfs_demo.nfs_retention_audit"

    databricks_profile: str = ""
    databricks_warehouse_id: str = "50d2bd4ddf65e0f5"
    # Statement Execution API caps combined parameter size at 1,048,576
    # characters; keep each JSON row parameter safely below it.
    databricks_max_parameter_chars: int = 900_000

    # Databricks secret scope holding NMS/SWIM credentials (see _SECRET_KEYS).
    nfs_secret_scope: str = "nfs"

    def missing_credentials(self) -> list[str]:
        """Credential fields not supplied by .env, environment or constructor."""
        return [
            field
            for field in _SECRET_KEYS
            if field not in self.model_fields_set or not getattr(self, field)
        ]

    def apply_secret_scope(self, client=None) -> list[str]:
        """Fill credential fields still unset from the Databricks secret scope.

        .env values, environment variables and constructor values always win.
        If they already supply every credential (e.g. a .env file when running
        outside Databricks), the secret scope and the Databricks SDK are not
        used at all. Set NFS_SECRET_SCOPE=none to disable the lookup.

        Call this explicitly from job entry points; it is never run at import
        time so tests and local runs stay offline. Returns the settings fields
        that were set from the secret scope.
        """
        missing = self.missing_credentials()
        if not missing:
            logger.info("All credentials supplied locally; secret scope not used.")
            return []
        if self.nfs_secret_scope.strip().lower() in ("", "none"):
            logger.info("Secret scope disabled; missing credentials: %s", missing)
            return []
        try:
            if client is None:
                from databricks.sdk import WorkspaceClient

                client = WorkspaceClient(profile=self.databricks_profile or None)
        except Exception as exc:  # noqa: BLE001 - optional convenience path
            logger.warning("Secret scope lookup unavailable: %s", exc)
            return []

        applied: list[str] = []
        for field in missing:
            try:
                secret = client.secrets.get_secret(
                    scope=self.nfs_secret_scope,
                    key=_SECRET_KEYS[field],
                )
            except Exception:  # noqa: BLE001 - missing keys are allowed
                continue
            if secret and secret.value:
                value = base64.b64decode(secret.value).decode("utf-8").strip()
                if value:
                    setattr(self, field, value)
                    applied.append(field)
        return applied


settings = Settings()
