"""Connections to the Lakebase PostGIS gold database (schema ``aviation``).

Authentication uses short-lived Databricks OAuth database credentials, never a
stored password. In Databricks Apps the app's service principal is used
(DATABRICKS_CLIENT_ID/SECRET are injected, and PGUSER/PGHOST are set when the
Lakebase project is attached as an app resource); in jobs and notebooks the
current Databricks identity is used.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from server.config import settings
from server.tool_resources.constants import (
    TOKEN_REFRESH_MARGIN_SECONDS,
    TOKEN_TTL_SECONDS,
)


@dataclass(frozen=True)
class GoldTarget:
    host: str
    database: str
    endpoint: str

    @classmethod
    def from_settings(cls) -> GoldTarget:
        return cls(
            host=os.getenv("PGHOST") or settings.lakebase_host,
            database=os.getenv("PGDATABASE") or settings.lakebase_database,
            endpoint=(
                f"projects/{settings.lakebase_project_id}"
                f"/branches/{settings.lakebase_branch_id}"
                f"/endpoints/{settings.lakebase_endpoint_id}"
            ),
        )


class _CredentialCache:
    """Caches (user, token) and refreshes it before expiry. Thread safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._user: str | None = None
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, endpoint: str) -> tuple[str, str]:
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
                return self._user or "", self._token
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient()
            # App service principals log in as their client id; users by email.
            user = os.getenv("PGUSER") or os.getenv("DATABRICKS_CLIENT_ID")
            if not user:
                user = client.current_user.me().user_name
            credential = client.postgres.generate_database_credential(
                endpoint=endpoint
            )
            self._user, self._token = user, credential.token
            self._expires_at = now + TOKEN_TTL_SECONDS
            return user, credential.token


_credentials = _CredentialCache()


@contextmanager
def connect(
    *, autocommit: bool = True, statement_timeout_ms: int | None = 30_000
) -> Iterator[Any]:
    """Open a psycopg connection to the gold database.

    Reads run with a statement timeout so a slow spatial query fails cleanly
    instead of hanging an MCP tool call.
    """
    import psycopg

    target = GoldTarget.from_settings()
    user, token = _credentials.get(target.endpoint)
    options = (
        f"-c statement_timeout={statement_timeout_ms}" if statement_timeout_ms else ""
    )
    with psycopg.connect(
        host=target.host,
        port=int(os.getenv("PGPORT") or 5432),
        dbname=target.database,
        user=user,
        password=token,
        sslmode="require",
        connect_timeout=30,
        autocommit=autocommit,
        options=options,
        application_name=os.getenv("PGAPPNAME") or "nfs-mcp",
    ) as connection:
        yield connection


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run one read query and return rows as dicts."""
    from psycopg.rows import dict_row

    with connect() as connection, connection.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or {})
        return list(cur.fetchall())
