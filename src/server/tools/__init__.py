"""Register all tools exposed by the server."""

from server.tools import (
    flight_briefing,
    flight_plans,
    flights_approaching,
    route_deviations,
    route_notams,
    search_notams,
)


def register_tools(mcp) -> None:
    """Register every tool module."""
    route_notams.register(mcp)
    flight_plans.register(mcp)
    flights_approaching.register(mcp)
    flight_briefing.register(mcp)
    route_deviations.register(mcp)
    search_notams.register(mcp)


__all__ = ["register_tools"]
