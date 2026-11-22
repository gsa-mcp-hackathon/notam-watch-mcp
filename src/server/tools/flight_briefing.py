"""get_flight_briefing: everything about one live flight."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from server.models import Callsign, Limit
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the get_flight_briefing tool."""

    @mcp.tool(
        name="get_flight_briefing",
        annotations={"title": "Live flight briefing", **READ_ONLY_TOOL},
    )
    async def get_flight_briefing(
        callsign: Callsign,
        corridor_nm: Annotated[
            float, Field(default=10, ge=1, le=50,
                         description="Corridor half-width for NOTAMs ahead.")
        ] = 10,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """Everything known about one live flight: position, altitude, speed,
        aircraft type with FAA characteristics (wingspan, weight class,
        approach category), filed route and ETA, how far it is off its planned
        route right now, NOTAM hazard areas ahead on the remaining route, and
        destination airport NOTAMs around its ETA.

        Use for "what should DAL636 know about?" or to check whether a
        specific flight is conforming to its plan.
        """
        return await run_tool_query(
            q.flight_briefing, callsign=callsign, corridor_nm=corridor_nm, limit=limit
        )
