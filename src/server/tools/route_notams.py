"""get_route_notams: pre-flight NOTAM briefing for a trip A -> B."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from server.models import AirportCode, IsoTime, Limit
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the get_route_notams tool."""

    @mcp.tool(
        name="get_route_notams",
        annotations={"title": "NOTAMs for a planned flight", **READ_ONLY_TOOL},
    )
    async def get_route_notams(
        origin: AirportCode,
        destination: AirportCode,
        departure_time: IsoTime = None,
        cruise_altitude_ft: Annotated[
            int | None,
            Field(default=None, ge=1000, le=60000,
                  description="Planned cruise altitude in feet (FL350 = 35000)."),
        ] = None,
        callsign: Annotated[
            str | None,
            Field(default=None, max_length=10,
                  description="Optional filed callsign to use that flight's "
                  "TFMS planned route instead of a typical one."),
        ] = None,
        corridor_nm: Annotated[
            float,
            Field(default=10, ge=1, le=50,
                  description="Half-width of the route corridor in NM."),
        ] = 10,
        include_obstacles: Annotated[
            bool,
            Field(default=False,
                  description="Include obstacle NOTAMs away from the airports."),
        ] = False,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """Pre-flight NOTAM briefing for a flight from origin to destination.

        Use when a pilot or dispatcher asks which NOTAMs matter for a trip.
        Builds the route from the flight's filed TFMS route (callsign), else a
        recent filed route for the same city pair, else the great circle. For
        each NOTAM hazard area in the corridor it returns: whether the route
        crosses it, distance off route, where along the route, estimated pass
        time and altitude, phase of flight, and whether the NOTAM is vertically
        overlapping then. Confirmed schedule-inactive hazards at the estimated
        pass time are excluded; unresolved schedules remain visible. Also
        returns departure and arrival airport NOTAMs (runways, ILS, approaches)
        whose schedules overlap those windows. Rank and explain them for the
        user; do not assume every listed NOTAM is a conflict.
        """
        departure = (
            q.parse_time(departure_time, datetime.now(UTC)) if departure_time else None
        )
        return await run_tool_query(
            q.route_notams,
            origin_code=origin,
            destination_code=destination,
            departure_time=departure,
            cruise_altitude_ft=cruise_altitude_ft,
            corridor_nm=corridor_nm,
            callsign=callsign,
            include_obstacles=include_obstacles,
            limit=limit,
        )
