"""Shared utility functions and objects used across the MCP server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# MCP tool annotations: all tools are read-only queries.
READ_ONLY_TOOL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


# ---------------------------------------------------------------- formatters


def iso(value: Any) -> str | None:
    """Format a datetime as ISO-8601 UTC; pass through strings; None -> None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def rounded(value: Any, digits: int = 1) -> float | None:
    """Round to *digits* decimal places; None -> None."""
    return None if value is None else round(float(value), digits)


def parse_time(value: str | None, default: datetime) -> datetime:
    """Parse an ISO-8601 time; naive values are UTC."""
    if not value:
        return default
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------- tool-layer helpers


def fetch_gold(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute a read query against the Lakebase PostGIS gold database."""
    from server.tool_resources.services.gold_db import fetch_all

    return fetch_all(sql, params)


async def run_tool_query(
    func: Callable[..., dict[str, Any]], **kwargs: Any
) -> dict[str, Any]:
    """Run a blocking query off the event loop; attach data freshness and
    turn failures into a clear message for the agent."""
    from server.tool_resources import queries as q

    def call() -> dict[str, Any]:
        result = func(fetch=fetch_gold, **kwargs)
        result["data_status"] = q.data_status(fetch_gold)
        return result

    try:
        return await asyncio.to_thread(call)
    except ValueError as exc:
        return {"error": f"Invalid input: {exc}"}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the session
        logger.exception("gold query failed: %s", func.__name__)
        return {
            "error": "The aviation gold database query failed.",
            "detail": str(exc)[:500],
        }
