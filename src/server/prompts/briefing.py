"""Conversation starters that steer agents to the flight/NOTAM tools."""

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register briefing prompts."""

    @mcp.prompt()
    def preflight_notam_briefing(
        origin: str,
        destination: str,
        departure_time: str = "now",
        cruise_altitude_ft: int | None = None,
    ) -> str:
        """Ask which aviation safety notices matter most for a planned trip."""
        altitude_instruction = (
            f"Pass cruise_altitude_ft={cruise_altitude_ft} to the tool. "
            if cruise_altitude_ft is not None
            else ""
        )
        return (
            f"I am planning a flight from {origin} to {destination}, departing "
            f"{departure_time}. Use get_route_notams to find the Notices to Air "
            "Missions (NOTAMs) along the route and at both airports. "
            f"{altitude_instruction}Rank the notices that deserve attention "
            "first and explain why using route, "
            "altitude, and timing evidence. Explain each NOTAM in plain English "
            "from its text, taking altitudes and times from the tool output. "
            "Note the data_status freshness."
        )

    @mcp.prompt()
    def airspace_watch(area: str, horizon_minutes: int = 30) -> str:
        """Ask which flights may soon enter an active airspace restriction."""
        return (
            f"Which live flights are currently inside, or may enter within "
            f"{horizon_minutes} minutes, an active NOTAM airspace restriction "
            f"near {area}? "
            "Use get_flights_approaching_notams, then summarize each flight's "
            "time to entry, separating confirmed conflicts from unknown "
            "altitude relationships. Note the data_status freshness."
        )

    @mcp.prompt()
    def flight_status(callsign: str) -> str:
        """Ask for a plain-language safety briefing on one live flight."""
        return (
            f"Give me a plain-language briefing for flight {callsign}: current "
            "position, "
            "altitude, speed, aircraft type and characteristics, filed route "
            "and ETA, whether it is on or off its plan, NOTAM hazard areas "
            "ahead on the remaining route, and destination airport NOTAMs. "
            "Use get_flight_briefing. Note the data_status freshness."
        )

    @mcp.prompt()
    def route_conformance() -> str:
        """Look nationwide for flights off their planned route near NOTAMs."""
        return (
            "Which flights nationwide are off their planned routes near active "
            "NOTAM airspace? Use find_route_deviations_near_notams. "
            "For each deviating flight, explain how far it is off plan, the "
            "nearest active NOTAM, and whether its original route crossed that "
            "NOTAM area. Flag candidates for review without inferring why "
            "the flight changed course. Note the data_status freshness."
        )

    @mcp.prompt()
    def notam_lookup(query: str) -> str:
        """Search for aviation safety notices using an everyday description."""
        return (
            f"Find current NOTAMs matching: {query}. "
            "Use search_notams with appropriate filters (location, text, "
            "Q-code prefix, or nearby airport). Explain each result in plain "
            "English from its text, noting altitude bands, effective times, "
            "and schedules. Note the data_status freshness."
        )
