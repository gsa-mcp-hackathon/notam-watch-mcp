"""Shared constants used across the MCP server.

Distances are nautical miles; altitudes are feet.
"""

from __future__ import annotations

from datetime import timedelta

# Unit conversions.
METERS_PER_NM = 1852.0
HOUR = timedelta(hours=1)

# Typical jet climb/descent gradient used to estimate altitude along a route.
CLIMB_DESCENT_FT_PER_NM = 300.0
DEFAULT_CRUISE_FT = 35000.0
DEFAULT_SPEED_KTS = 420.0
# Vertical buffer either side of the estimated altitude.
VERTICAL_BUFFER_FT = 1000.0
# Obstacles are only relevant near the departure/arrival airports by default.
TERMINAL_RADIUS_NM = 10.0
# Filed/assigned altitudes below this are interim (e.g. a descent clearance),
# not a cruise altitude to plan with.
MIN_PLANNED_CRUISE_FT = 10000.0

# Max hazard areas evaluated per flights-approaching call.
MAX_WATCH_HAZARDS = 2000

# Airspace hazard families (``hazard_type``).
HAZARD_TYPES = ("tfr", "sua", "uas", "warning", "any")
# Families small enough to scan the whole country inside the 30 s statement
# timeout (measured 2026-10-29 on ~6,200 live flights: tfr 3-14 s / 212
# hazards incl. 120 min horizon, sua 9 s / 268, warning 1 s / 82). uas
# (~900 areas, mostly near airports) and any (~1,500) time out nationwide,
# so they need an area anchor.
NATIONWIDE_HAZARD_TYPES = ("tfr", "sua", "warning")
