"""get_flights_approaching_notams: live flights entering NOTAM airspace."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from server.models import Limit
from server.tool_resources import queries as q
from server.utils import READ_ONLY_TOOL, fetch_gold, run_tool_query


def register(mcp: FastMCP) -> None:
    """Register the get_flights_approaching_notams tool."""

    @mcp.tool(
        name="get_flights_approaching_notams",
        annotations={"title": "Flights entering NOTAM airspace", **READ_ONLY_TOOL},
    )
    async def get_flights_approaching_notams(
        hazard_type: Annotated[
            Literal["tfr", "sua", "uas", "warning", "any"] | None,
            Field(default=None,
                  description="Hazard family: 'tfr' (temporary flight "
                  "restrictions), 'sua' (restricted/warning/MOA/prohibited "
                  "activations), 'uas' (drone areas), 'warning' (QW navigational "
                  "warnings), 'any'. With NO location given, 'tfr', 'sua' and "
                  "'warning' scan the whole country in ONE call; 'uas' and 'any' "
                  "need an airport or latitude/longitude."),
        ] = None,
        notam_id: Annotated[
            str | None,
            Field(default=None, max_length=20,
                  description="Watch one NOTAM by id, e.g. 'A0071/26' (as returned "
                  "by search_notams)."),
        ] = None,
        notam_location: Annotated[
            str | None,
            Field(default=None, max_length=5,
                  description="Location of that NOTAM (e.g. 'ZNY', 'KJFK'); "
                  "domestic NOTAM numbers like '10/653' repeat across locations."),
        ] = None,
        airspace: Annotated[
            str | None,
            Field(default=None, max_length=40,
                  description="Special use airspace by designator or name, e.g. "
                  "'R-5206', 'W-570B', 'LIVE OAK MOA'. Watches its NOTAM "
                  "activations."),
        ] = None,
        airport: Annotated[
            str | None,
            Field(default=None, max_length=4,
                  description="Watch NOTAM areas around this airport."),
        ] = None,
        latitude: Annotated[
            float | None, Field(default=None, ge=-90, le=90,
                                description="Watch-area center latitude.")
        ] = None,
        longitude: Annotated[
            float | None, Field(default=None, ge=-180, le=180,
                                description="Watch-area center longitude.")
        ] = None,
        radius_nm: Annotated[
            float, Field(default=50, ge=1, le=300,
                         description="Watch-area radius in NM.")
        ] = 50,
        horizon_minutes: Annotated[
            int, Field(default=30, ge=1, le=120,
                       description="How far ahead to project each flight.")
        ] = 30,
        include_obstacles: Annotated[bool, Field(default=False)] = False,
        only_vertical_conflicts: Annotated[
            bool,
            Field(default=True,
                  description="Drop flights clearly above/below the NOTAM's "
                  "altitude band at both current and planned altitude."),
        ] = True,
        limit: Limit = 25,
    ) -> dict[str, Any]:
        """Airspace watch: live flights inside, or on course to enter, active
        NOTAM hazard areas (TFRs, restricted/special-use airspace, UAS areas,
        etc.) within the next horizon_minutes.

        Use for controller/operations questions like "which flights are
        about to enter any TFR?" (hazard_type='tfr', nothing else - one call
        covers every active TFR nationwide; do NOT call search_notams first or
        loop per NOTAM), "which flights are heading into restricted area
        R-5206?" (airspace='R-5206') or "anything about to enter NOTAM
        airspace near DCA?" (airport='DCA', optionally with hazard_type).
        Give hazard_type alone (tfr/sua/warning), or a notam_id, an airspace,
        an airport, or a latitude/longitude (with radius_nm), optionally
        combined with hazard_type. Special use airspace activations (!SUA NOTAMs) are
        drawn with the FAA's charted SUA boundary. Each flight is projected
        along its TFMS planned route (or its current track if off-plan);
        results give minutes and distance to entry, current vs planned
        altitude relative to the NOTAM's altitude band, whether the NOTAM's
        daily/weekday schedule applies at entry, and the NOTAM text.
        NOTAMs publishing an identical area are evaluated separately, then
        merged per flight; hazard.same_area_notam_evidence retains each
        NOTAM's schedule and altitude assessment. counts splits encounters
        into 'confirmed' (altitude overlaps the band) and 'altitude_unknown'
        (including AGL bands that need terrain elevation); report both numbers,
        not just the total. Confirmed are listed first.
        """
        anchored = bool(notam_id or airspace or airport) or (
            latitude is not None and longitude is not None
        )
        if not anchored and hazard_type not in q.NATIONWIDE_HAZARD_TYPES:
            return {
                "error": "No watch area given.",
                "how_to_fix": "For a nationwide scan call this tool again with "
                "hazard_type='tfr', 'sua' or 'warning' and no location. "
                "Otherwise give airport, airspace, notam_id or "
                "latitude+longitude (hazard_type 'uas'/'any' need one of these). "
                "Do not call search_notams first.",
            }
        sua = None
        if airspace:
            sua = await asyncio.to_thread(q.resolve_sua, airspace, fetch_gold)
            if not sua:
                return {"error": f"Unknown special use airspace: {airspace}."}
        lon, lat = longitude, latitude
        if airport and (lat is None or lon is None):
            found = await asyncio.to_thread(q.resolve_airport, airport, fetch_gold)
            if not found:
                return {"error": f"Unknown airport identifier: {airport}."}
            lon, lat = found["lon"], found["lat"]
        result = await run_tool_query(
            q.flights_approaching_hazards,
            notam_id=notam_id.strip() if notam_id else None,
            notam_location=notam_location,
            sua_designator=sua["designator"] if sua else None,
            center_lon=lon,
            center_lat=lat,
            radius_nm=radius_nm,
            horizon_minutes=horizon_minutes,
            include_obstacles=include_obstacles,
            only_vertical_conflicts=only_vertical_conflicts,
            limit=limit,
            hazard_type=hazard_type,
        )
        if sua:
            result["airspace"] = q.sua_summary(sua, datetime.now(UTC))
            if not result.get("encounters") and not result.get("error"):
                in_times = result["airspace"]["published_times"][
                    "in_published_times"
                ]
                result["airspace"]["note"] = (
                    "No live flight conflicts with an active NOTAM activation of "
                    "this airspace. "
                    + {
                        True: "It is inside its published times of use now, so it "
                        "may be in use without a NOTAM-derived area here; check "
                        "with the controlling agency.",
                        False: "It is outside its published times of use now.",
                        None: "Whether it is inside its published times of use "
                        "could not be determined.",
                    }[in_times]
                )
        return result
