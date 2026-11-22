"""Static information about the NFS MCP server."""

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register server information resources."""

    @mcp.resource("nfs://info/server")
    def server_info() -> str:
        """Describe the server and its available capabilities."""
        return (
            "NFS MCP Server\n\n"
            "find_flight_plan: match a callsign to filed TFMS flight plans.\n"
            "get_route_notams: NOTAMs that matter for a planned trip A->B.\n"
            "get_flights_approaching_notams: live flights in or heading into "
            "NOTAM airspace.\n"
            "get_flight_briefing: one live flight with aircraft, plan and "
            "NOTAMs ahead.\n"
            "find_route_deviations_near_notams: off-plan flights near NOTAM "
            "areas.\n"
            "search_notams: look up current NOTAMs.\n"
            "Successful tool results include data_status (gold-layer freshness).\n"
            "Weather alerting and ground-stop recovery are not implemented."
        )
