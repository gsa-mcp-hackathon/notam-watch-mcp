"""Constants shared across tool_resources subdirectories."""

from __future__ import annotations

import re

# --------------------------------------------------------- calendar lookups
# Used by both schedule parsers (notam_schedule and sua_schedule).

WEEKDAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAY_NAMES)}

MONTH_NAMES = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)
MONTH_INDEX = {name: i for i, name in enumerate(MONTH_NAMES, start=1)}

# Schedule parser sentinel: an "all day" time range.
ALL_DAY_TIMES = ("0000", "2400")

# ------------------------------------------- NOTAM schedule tokenizer patterns

# Regex building blocks for the NOTAM item-D schedule tokenizer.
NOTAM_WD_PATTERN = "|".join(WEEKDAY_NAMES)
NOTAM_MON_PATTERN = "|".join(MONTH_INDEX)
NOTAM_EDGE_PATTERN = r"(?:\d{4}|SR|SS)(?:\s*(?:PLUS|MINUS)\s*\d{1,3})?"

# Compiled tokenizer for NOTAM schedule strings.
NOTAM_TOKEN_RE = re.compile(
    rf"""
    (?P<stamp>\d{{10}})(?!\d)
  | (?P<time>{NOTAM_EDGE_PATTERN}\s*[-/]\s*{NOTAM_EDGE_PATTERN})
  | (?P<xrange>\d{{1,2}}\s*-\s*(?:{NOTAM_MON_PATTERN})\s*\d{{1,2}})(?!\d)
  | (?P<wdrange>(?:{NOTAM_WD_PATTERN})\s*-\s*(?:{NOTAM_WD_PATTERN}))\b
  | (?P<wd>{NOTAM_WD_PATTERN})\b
  | (?P<month>{NOTAM_MON_PATTERN})\b
  | (?P<dayrange>\d{{1,2}}\s*-\s*\d{{1,2}})(?!\d)
  | (?P<day>\d{{1,2}})(?!\d)
  | (?P<daily>DLY|DAILY)\b
  | (?P<h24>H24)\b
  | (?P<exc>EXC|EXCEPT)\b
  | (?P<sep>[,;.])
  | (?P<skip>BTN|AND|ONLY|FROM|TO|UTC|ON|EVERY|FM|TIL|UNTIL)\b
  | (?P<ws>\s+)
  | (?P<other>\S+)
    """,
    re.VERBOSE,
)

# Normalizations applied before tokenizing (ICAO abbreviations, noise).
NOTAM_REWRITES = (
    (re.compile(r"(\d{4})Z\b"), r"\1"),
    (re.compile(r"\bHRS?:\s*"), ""),
    (re.compile(r"\bHJ\b"), "SR-SS"),
    (re.compile(r"\bHN\b"), "SS-SR"),
    (re.compile(r"\bMO(?=\s*-|\b)"), "MON"),
    (re.compile(r"\bTU(?=\s*-|\b)"), "TUE"),
    (re.compile(r"\bWE(?=\s*-|\b)"), "WED"),
    (re.compile(r"\bTH(?=\s*-|\b)"), "THU"),
    (re.compile(r"\bFR(?=\s*-|\b)"), "FRI"),
)

# ------------------------------------------------ SUA / AIXM day-type rules

# AIXM CodeDayType values that cannot be resolved to concrete weekdays.
PROHIBITED_DAY_VALUES = frozenset(
    {"WORK_DAY", "BEF_WORK_DAY", "AFT_WORK_DAY", "HOL", "BEF_HOL", "AFT_HOL",
     "BUSY_FRI", "LH"}
)

# US timezone offset rules for SUA published times.
# Offsets that never observe daylight saving.
NO_DST_OFFSETS = frozenset({-4.0, -10.0, -11.0, 10.0})
# Offsets ambiguous during US DST (Arizona vs. rest of Mountain).
AMBIGUOUS_DST_OFFSETS = frozenset({-7.0})

# ------------------------------------------- Lakebase credential cache timing

TOKEN_TTL_SECONDS = 3600
TOKEN_REFRESH_MARGIN_SECONDS = 600

# ------------------------------------------------- hazard type SQL predicates
# Predicates reference aviation.notam_hazards h / aviation.notams n.
# '%%' is a literal '%' for psycopg.

HAZARD_TYPE_SQL = {
    # Temporary restricted areas (Q-code QRT*) and FDC TFR text.
    "tfr": "(n.category LIKE 'QRT%%' "
    "OR n.raw_text ILIKE '%%TEMPORARY FLIGHT RESTRICTION%%')",
    # Special use airspace activations: charted SUA boundary or restricted,
    # danger, prohibited, MOA, overflight Q-codes.
    "sua": "(h.geometry_source = 'SUA_REFERENCE' OR n.category ~ '^QR[ADPMO]')",
    # Drone / unmanned aircraft operating areas.
    "uas": "(n.category LIKE 'QWU%%' OR n.raw_text ~* '\\m(UAS|UNMANNED)\\M')",
    # Navigational warnings (QW*: parachuting, aerobatics, blasting, UAS, ...).
    "warning": "(n.category LIKE 'QW%%')",
    "any": "TRUE",
}

# ----------------------------------------------- shared SQL template fragments
# Inserted where a .sql file says {name}. Values are SQL text, never user
# input (that goes in %(params)s).

SQL_FRAGMENTS = {
    # Hazard columns every hazard-returning query selects (see
    # hazard_summary). h = aviation.notam_hazards, n = aviation.notams.
    "hazard_fields": """
    h.hazard_id, h.notam_id, h.location, n.category AS q_code,
    h.geometry_source, h.hazard_class, h.is_obstacle,
    h.lower_altitude_ft, h.upper_altitude_ft,
    h.lower_altitude_reference, h.upper_altitude_reference,
    h.effective_start, h.effective_end, h.parse_confidence,
    left(n.raw_text, 400) AS text, n.schedule,
    ST_X(ST_PointOnSurface(h.geom)) AS hz_lon,
    ST_Y(ST_PointOnSurface(h.geom)) AS hz_lat
""",
    # Extra NOTAM predicate in every query. Synthetic NMS staging test NOTAMs
    # are already excluded at publish time (a per-query text filter here
    # defeated the spatial index), so this is a no-op hook.
    "not_test": "TRUE",
    # Airport NOTAM subjects most relevant to operations, by Q-code prefix.
    "airport_priority": (
        "ARRAY['QMR','QFA','QIC','QIS','QIL','QIG','QPI','QPD','QMX','QLR',"
        "'QLA','QOB','QFF']"
    ),
    # Columns of a planned route (aviation.flight_routes).
    "route_columns": """
        ST_AsEWKT(geom) AS wkt, ST_Length(geom::geography) / 1852 AS length_nm,
        callsign, route_string, planned_altitude_ft, filed_true_airspeed_kts,
        etd_time, eta_time, departure_procedure, arrival_procedure
    """,
}
