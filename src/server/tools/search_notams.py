"""search_notams: direct NOTAM lookup by location, text, Q-code, distance."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from server.models import IsoTime, Limit
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, fetch_gold, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the search_notams tool."""

    @mcp.tool(
        name="search_notams",
        annotations={"title": "Search current NOTAMs", **READ_ONLY_TOOL},
    )
    async def search_notams(
        location: Annotated[
            str | None,
            Field(default=None, max_length=5,
                  description="NOTAM location / airport, e.g. KJFK or JFK."),
        ] = None,
        text_contains: Annotated[
            str | None,
            Field(default=None, max_length=80,
                  description="Case-insensitive text match, e.g. 'RWY 04L'."),
        ] = None,
        q_code_prefix: Annotated[
            str | None,
            Field(default=None, max_length=5, pattern=r"^[Qq][A-Za-z]{0,4}$",
                  description="ICAO Q-code prefix, e.g. QMR (runway), QRT "
                  "(temporary restricted area), QW (warnings)."),
        ] = None,
        near_airport: Annotated[
            str | None,
            Field(default=None, max_length=4,
                  description="Find NOTAMs located within radius_nm of this "
                  "airport."),
        ] = None,
        radius_nm: Annotated[float, Field(default=25, ge=1, le=300)] = 25,
        active_at: IsoTime = None,
        include_obstacles: Annotated[bool, Field(default=False)] = False,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """Search current FAA NOTAMs by location, text, Q-code and/or distance
        from an airport, active at a given time (default now).

        Use for direct lookups ("runway closures at KORD", "UAS NOTAMs near
        DCA"). For trip planning prefer get_route_notams; for live traffic
        prefer get_flights_approaching_notams.
        """
        if not any((location, text_contains, q_code_prefix, near_airport)):
            return {"error": "Provide location, text_contains, q_code_prefix or "
                    "near_airport."}
        near_lon = near_lat = None
        if near_airport:
            found = await asyncio.to_thread(q.resolve_airport, near_airport, fetch_gold)
            if not found:
                return {"error": f"Unknown airport identifier: {near_airport}."}
            near_lon, near_lat = found["lon"], found["lat"]
        return await run_tool_query(
            q.search_notams,
            location=location,
            text_contains=text_contains,
            q_code_prefix=q_code_prefix,
            near_lon=near_lon,
            near_lat=near_lat,
            radius_nm=radius_nm,
            active_at=q.parse_time(active_at, datetime.now(UTC)),
            include_obstacles=include_obstacles,
            limit=limit,
        )
