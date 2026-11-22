"""find_route_deviations_near_notams: off-plan flights near NOTAM areas."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from server.models import Limit
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the find_route_deviations_near_notams tool."""

    @mcp.tool(
        name="find_route_deviations_near_notams",
        annotations={"title": "Off-plan flights near NOTAMs", **READ_ONLY_TOOL},
    )
    async def find_route_deviations_near_notams(
        min_deviation_nm: Annotated[
            float, Field(default=5, ge=1, le=200,
                         description="Minimum cross-track distance from plan.")
        ] = 5,
        near_notam_nm: Annotated[
            float, Field(default=30, ge=1, le=200,
                         description="Search radius for active NOTAM areas.")
        ] = 30,
        require_nearby_notam: Annotated[
            bool, Field(default=True,
                        description="Only flights with an active NOTAM nearby.")
        ] = True,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """Route-conformance scan: live flights that are off their TFMS
        planned route, paired with the nearest active NOTAM hazard area and
        whether their original plan crossed it.

        Use for analyst questions such as "are flights deviating around the
        new TFR?" or "who is not following their filed route near NOTAM
        airspace?". A deviating flight whose plan crosses an active NOTAM area
        is a candidate avoidance/reroute; explain with that caveat.
        """
        return await run_tool_query(
            q.route_deviations,
            min_deviation_nm=min_deviation_nm,
            near_notam_nm=near_notam_nm,
            require_nearby_notam=require_nearby_notam,
            limit=limit,
        )
