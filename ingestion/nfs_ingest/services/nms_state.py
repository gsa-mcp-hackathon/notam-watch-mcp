"""SQL identifier and literal helpers shared by the Delta/SQL writers.

The NMS watermark itself is managed by
``nfs_ingest.services.nms_storage.NmsSqlWatermarkStore``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _qualified_identifier(value: str) -> str:
    parts = value.split(".")
    if len(parts) != 3 or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("Watermark table must be a three-part Unity Catalog name.")
    return ".".join(f"`{part}`" for part in parts)


def _string_literal(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _timestamp_literal(value: datetime | None) -> str:
    if value is None:
        return "NULL"
    if value.tzinfo is None:
        raise ValueError("Watermark timestamps must include a timezone.")
    normalized = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    return f"TIMESTAMP '{normalized}'"
