"""find_flight_plan: resolve a callsign to candidate filed TFMS plans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from server.models import AirportCode, Callsign, IsoTime
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the find_flight_plan tool."""

    @mcp.tool(
        name="find_flight_plan",
        annotations={"title": "Find filed flight plans", **READ_ONLY_TOOL},
    )
    async def find_flight_plan(
        callsign: Callsign,
        origin: AirportCode | None = None,
        destination: AirportCode | None = None,
        departure_time: IsoTime = None,
        window_minutes: Annotated[
            int,
            Field(
                default=360,
                ge=15,
                le=1440,
                description="Search this many minutes before and after departure_time.",
            ),
        ] = 360,
        limit: Annotated[
            int, Field(default=10, ge=1, le=25, description="Max candidate plans.")
        ] = 10,
    ) -> dict[str, Any]:
        """Find current or future filed TFMS plans for a callsign.

        Use before get_route_notams when a user gives an airline/ATC callsign
        but does not know the destination. Origin and destination are optional
        disambiguators. departure_time defaults to now and the search uses a
        symmetric time window. Results may include a filed plan without a live
        aircraft position. This is not an airline schedule lookup; if no TFMS
        plan is present, report that limitation rather than guessing.
        """
        departure = q.parse_time(departure_time, datetime.now(UTC))
        return await run_tool_query(
            q.find_flight_plans,
            callsign=callsign,
            origin=origin,
            destination=destination,
            departure_time=departure,
            window_minutes=window_minutes,
            limit=limit,
        )
