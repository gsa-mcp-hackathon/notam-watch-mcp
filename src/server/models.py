"""Reusable Pydantic type aliases and data-model types."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

# Tool parameter types validated by FastMCP / Pydantic.
AirportCode = Annotated[
    str,
    Field(
        min_length=3,
        max_length=4,
        pattern=r"^[A-Za-z0-9]{3,4}$",
        description="Airport identifier, ICAO (KJFK) or FAA (JFK).",
    ),
]
Callsign = Annotated[
    str,
    Field(
        min_length=2,
        max_length=10,
        pattern=r"^[A-Za-z0-9]{2,10}$",
        description="ATC callsign / aircraft id as filed, e.g. DAL636, N123AB.",
    ),
]
IsoTime = Annotated[
    str | None,
    Field(
        default=None,
        description="ISO-8601 UTC time, e.g. 2026-10-29T14:00:00Z. Default: now.",
    ),
]
Limit = Annotated[int, Field(default=25, ge=1, le=100, description="Max items.")]

# Every query function takes ``fetch(sql, params) -> rows`` so it can be
# tested without a database (see tests/test_queries.py).
Fetch = Callable[[str, dict[str, Any]], list[dict[str, Any]]]
