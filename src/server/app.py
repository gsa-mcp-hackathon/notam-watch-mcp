"""Create the MCP server, register features, and select a transport."""

from __future__ import annotations

import os

from fastmcp import FastMCP

from server.config import settings
from server.prompts import register_prompts
from server.resources import register_resources
from server.routes import register_routes
from server.tools import register_tools

mcp = FastMCP(
    name="NFS MCP Server",
    instructions=(
        "Live FAA aviation context: current NOTAMs (FAA NMS) with mapped "
        "hazard areas and altitude bands, live flight positions and filed "
        "TFMS routes (FAA SWIM R14), NASR airports and the FAA Aircraft "
        "Characteristics Database, served from a PostGIS database. "
        "find_flight_plan: resolve a callsign and approximate departure time "
        "to candidate filed TFMS plans, including plans without a live position. "
        "get_route_notams: NOTAMs that matter for a trip A->B. "
        "get_flights_approaching_notams: live flights in or heading into "
        "NOTAM airspace. get_flight_briefing: one live flight in detail. "
        "find_route_deviations_near_notams: off-plan flights near NOTAM areas. "
        "search_notams: direct NOTAM lookup. Special use airspace activations "
        "(R-, W-, A-, P- areas, MOAs) use the FAA's charted SUA boundaries, and "
        "NOTAM daily/weekday schedules (e.g. 'DLY 1230-2330') decide whether a "
        "NOTAM applies at a given time. Confirmed inactive schedules are "
        "excluded from applicable-only results; unresolved syntax remains "
        "visible as unknown. AGL altitude bands remain vertically unresolved "
        "without terrain elevation. Tools return evidence and "
        "assumptions, not decisions; check data_status for feed freshness. "
        "Responses include raw NOTAM text; translate it into plain English "
        "yourself, using the parsed altitudes, times and areas the tools "
        "return for any numbers."
    ),
)

register_tools(mcp)
register_prompts(mcp)
register_resources(mcp)
register_routes(mcp)


def main() -> None:
    """Run over HTTP on hosted platforms and stdio during local development."""
    platform_port = os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT")

    if platform_port:
        mcp.run(transport="http", host="0.0.0.0", port=int(platform_port))
    elif settings.mcp_transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
