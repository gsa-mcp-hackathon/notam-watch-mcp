"""Named special use airspace (R-5206, LIVE OAK MOA): lookup and charted
facts, used by get_flights_approaching_notams(airspace=...)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from server.tool_resources.queries.common import Fetch, band_text, sql
from server.tool_resources.services.sua_schedule import published_times_status


def resolve_sua(text: str, fetch: Fetch) -> dict[str, Any] | None:
    """Find a special use airspace by designator ('R-5206', 'W570B') or name
    ('LIVE OAK MOA', 'Fallon North 2 MOA')."""
    raw = " ".join(text.upper().replace("'", "").split())
    designator = re.sub(r"[^A-Z0-9]", "", raw)
    rows = fetch(sql("sua_airspace"), {"designator": designator, "name": raw})
    return rows[0] if rows else None


def sua_summary(row: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
    """Charted facts for an airspace (published limits and times of use);
    with ``at``, whether that moment is inside the published times."""
    limits = {
        "lower_altitude_ft": row.get("lower_ft"),
        "lower_altitude_reference": row.get("lower_reference"),
        "upper_altitude_ft": row.get("upper_ft"),
        "upper_altitude_reference": row.get("upper_reference"),
    }
    return {
        "designator": row.get("designator"),
        "name": row.get("name"),
        "type": row.get("sua_type"),
        "published_limits": band_text(limits),
        "published_times": published_times_status(
            row.get("schedule"),
            at or datetime.now(UTC),
            lat=row.get("lat"),
            lon=row.get("lon"),
        ),
        "source": f"FAA SAA AIXM, NASR cycle {row.get('nasr_effective_date')}",
    }
