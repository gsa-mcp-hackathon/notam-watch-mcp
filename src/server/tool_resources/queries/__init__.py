"""Flight and NOTAM questions answered from the PostGIS gold layer.

One module per MCP tool (src/server/tool_resources/queries/ backs
src/server/tools/flight_notam.py):

    get_route_notams                  -> route_notams.route_notams
    find_flight_plans                 -> flight_plans.find_flight_plans
    get_flights_approaching_notams    -> flights_approaching.flights_approaching_hazards
                                         (+ airspace.resolve_sua / sua_summary)
    get_flight_briefing               -> flight_briefing.flight_briefing
    find_route_deviations_near_notams -> route_deviations.route_deviations
    search_notams                     -> search.search_notams

Each query's SQL lives in sql/<name>.sql with a comment header; shared
helpers (altitude bands, schedules, data_status) are in common.py.

Functions return evidence, not verdicts: geometry facts (distance off route,
crossing, entry time), time and altitude overlap, provenance and caveats.
The calling model decides what matters. All take ``fetch(sql, params) ->
rows`` so the logic is tested without a database.
"""

from server.tool_resources.queries.airports import airport_notams, resolve_airport
from server.tool_resources.queries.airspace import resolve_sua, sua_summary
from server.tool_resources.queries.common import (
    HAZARD_TYPES,
    MAX_WATCH_HAZARDS,
    NATIONWIDE_HAZARD_TYPES,
    Fetch,
    applies_at,
    band_text,
    data_status,
    expected_altitude,
    hazard_type_predicate,
    is_active,
    parse_time,
    vertical_relation,
)
from server.tool_resources.queries.flight_briefing import flight_briefing
from server.tool_resources.queries.flight_plans import find_flight_plans
from server.tool_resources.queries.flights_approaching import (
    flights_approaching_hazards,
)
from server.tool_resources.queries.route_deviations import route_deviations
from server.tool_resources.queries.route_notams import (
    assess_route_hazards,
    hazards_along_line,
    route_notams,
)
from server.tool_resources.queries.search import search_notams

__all__ = [
    "HAZARD_TYPES",
    "MAX_WATCH_HAZARDS",
    "NATIONWIDE_HAZARD_TYPES",
    "Fetch",
    "airport_notams",
    "applies_at",
    "assess_route_hazards",
    "band_text",
    "data_status",
    "expected_altitude",
    "flight_briefing",
    "find_flight_plans",
    "flights_approaching_hazards",
    "hazard_type_predicate",
    "hazards_along_line",
    "is_active",
    "parse_time",
    "resolve_airport",
    "resolve_sua",
    "route_deviations",
    "route_notams",
    "search_notams",
    "sua_summary",
    "vertical_relation",
]
