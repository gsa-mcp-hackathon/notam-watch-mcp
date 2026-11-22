"""MCP server settings from an optional .env file and environment variables.

Precedence, highest first, for every setting:

1. Values passed to ``Settings(...)`` explicitly.
2. The .env file (see ``env_files``). Empty values (``MCP_PORT=``) are
   ignored, so a copied .env.example never blanks a real value.
3. Process environment variables (e.g. app.yaml ``env``).
4. Field defaults.

The server needs no credentials: in Databricks Apps the app's service
principal signs in to Lakebase. Feed credentials and table names for the
ingestion jobs live in ``ingestion/nfs_ingest/config.py``, which extends
these settings.
"""

import os
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def env_files() -> tuple[str, ...]:
    """.env files to read; later files win, missing files are skipped.

    NFS_ENV_FILE (an environment variable) selects one file explicitly.
    Otherwise the repo-root .env is read, then ./.env in the working
    directory, which overrides it.
    """
    explicit = os.environ.get("NFS_ENV_FILE", "").strip()
    if explicit:
        return (explicit,)
    return (str(_REPO_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    """Transport and Lakebase (PostGIS gold layer) settings."""

    model_config = SettingsConfigDict(
        env_file=env_files(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # .env before process environment variables (pydantic's default is
        # the reverse), for every setting.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # PostGIS gold layer (Lakebase project / branch / endpoint / database).
    lakebase_project_id: str = "nfs-aviation-postgis"
    lakebase_branch_id: str = "production"
    lakebase_endpoint_id: str = "primary"
    lakebase_database: str = "databricks_postgres"
    lakebase_host: str = ""


settings = Settings()
