"""Namespace-tolerant parser for FAA SWIM TFM R14 XML messages."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _find_text(element: ET.Element, names: tuple[str, ...]) -> str | None:
    for child in element.iter():
        if _local_name(child) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _number(value: str | None) -> float:
    cleaned = re.sub(r"[^0-9.-]", "", value or "")
    try:
        return float(cleaned) if cleaned else 0.0
    except ValueError:
        return 0.0


def _altitude(value: str | None) -> float:
    number = _number(value)
    return number * 100.0 if 0 < number <= 600 else number


def _coordinates(element: ET.Element) -> tuple[float | None, float | None]:
    latitude = None
    longitude = None
    position_nodes = [
        child
        for child in element.iter()
        if _local_name(child) in {"position", "posrep", "trackinformation"}
    ]
    for node in position_nodes or [element]:
        for child in node.iter():
            name = _local_name(child)
            if name == "latitudedms":
                value = (
                    float(child.attrib.get("degrees", 0))
                    + float(child.attrib.get("minutes", 0)) / 60
                    + float(child.attrib.get("seconds", 0)) / 3600
                )
                latitude = -value if child.attrib.get("direction", "").upper() in {
                    "S",
                    "SOUTH",
                } else value
            elif name == "longitudedms":
                value = (
                    float(child.attrib.get("degrees", 0))
                    + float(child.attrib.get("minutes", 0)) / 60
                    + float(child.attrib.get("seconds", 0)) / 3600
                )
                longitude = -value if child.attrib.get(
                    "direction", ""
                ).upper() in {"W", "WEST"} else value
            elif name in {"latitudedecimal", "latitude", "lat"} and child.text:
                latitude = _number(child.text)
            elif name in {"longitudedecimal", "longitude", "lon"} and child.text:
                longitude = _number(child.text)
        if latitude is not None and longitude is not None:
            break
    return (
        round(latitude, 6) if latitude is not None else None,
        round(longitude, 6) if longitude is not None else None,
    )


def _find_element(
    element: ET.Element, names: tuple[str, ...]
) -> ET.Element | None:
    for child in element.iter():
        if _local_name(child) in names:
            return child
    return None


def _aircraft(message: ET.Element) -> dict[str, str]:
    """Aircraft type and categories from TFMS flight-plan style messages.

    Track reports do not carry the type; FlightModify/FlightTimes/FlightRoute/
    FlightCreate use ``aircraftModel`` and flightPlanInformation/amendments use
    ``aircraftSpecification`` (text = ICAO type designator, attributes =
    engine class, equipment qualifier, heavy flag). ``qualifiedAircraftId``
    carries aircraftCategory/userCategory on most message types.
    """
    # flightPlanInformation uses flightAircraftSpecs and amendments use
    # newFlightAircraftSpecs; Flight* messages use aircraftSpecification.
    specification = _find_element(
        message,
        ("aircraftspecification", "flightaircraftspecs", "newflightaircraftspecs"),
    )
    qualified = _find_element(message, ("qualifiedaircraftid",))
    spec_attrs = specification.attrib if specification is not None else {}
    qualified_attrs = qualified.attrib if qualified is not None else {}
    spec_text = (
        specification.text.strip()
        if specification is not None and specification.text
        else ""
    )
    aircraft_type = _find_text(message, ("aircraftmodel",)) or spec_text
    return {
        "aircraft_type": aircraft_type.strip().upper(),
        "aircraft_engine_class": (
            spec_attrs.get("aircraftEngineClass")
            or qualified_attrs.get("aircraftCategory")
            or ""
        ),
        "equipment_qualifier": spec_attrs.get("equipmentQualifier") or "",
        "special_aircraft_qualifier": (
            spec_attrs.get("specialAircraftQualifier") or ""
        ),
        "aircraft_category": qualified_attrs.get("aircraftCategory") or "",
        "user_category": qualified_attrs.get("userCategory") or "",
    }


def _float_attr(element: ET.Element, name: str) -> float | None:
    try:
        return float(element.attrib[name])
    except (KeyError, TypeError, ValueError):
        return None


def _int_attr(element: ET.Element, name: str) -> int | None:
    value = _float_attr(element, name)
    return int(value) if value is not None else None


def _sequenced(
    parent: ET.Element, name: str
) -> list[tuple[int, ET.Element]]:
    items = [
        (_int_attr(child, "sequenceNumber") or 0, child)
        for child in parent
        if _local_name(child) == name
    ]
    return sorted(items, key=lambda item: item[0])


def _texts(parent: ET.Element, name: str) -> list[str]:
    """Ordered element texts with consecutive duplicates removed."""
    values: list[str] = []
    for _, child in _sequenced(parent, name):
        text = (child.text or "").strip()
        if text and (not values or values[-1] != text):
            values.append(text)
    return values


def _time_attrs(message: ET.Element, name: str, type_attr: str) -> dict[str, str]:
    element = _find_element(message, (name,))
    if element is None:
        return {"type": "", "time": ""}
    return {
        "type": element.attrib.get(type_attr) or "",
        "time": element.attrib.get("timeValue") or "",
    }


def _planned_altitude(message: ET.Element) -> float | None:
    for name in ("assignedaltitude", "requestedaltitude"):
        element = _find_element(message, (name,))
        if element is not None:
            text = _find_text(element, ("simplealtitude",))
            if text:
                return _altitude(text) or None
    return None


def _planned_route(message: ET.Element) -> dict | None:
    """TFMS modeled route (flightTraversalData2) plus filed-route context.

    Waypoints are the planned 2-D path (lat/lon to 0.01 deg) with
    ``elapsedTime`` in seconds after departure; there is no per-waypoint
    altitude. Present on FlightSectors, FlightRoute, flight plans/amendments
    and some track reports. Returns None when no traversal data exists.
    """
    traversal = _find_element(message, ("flighttraversaldata2",))
    if traversal is None:
        return None
    waypoints = []
    for sequence, waypoint in _sequenced(traversal, "waypoint"):
        latitude = _float_attr(waypoint, "latitudeDecimal")
        longitude = _float_attr(waypoint, "longitudeDecimal")
        if latitude is None or longitude is None:
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        waypoints.append(
            {
                "seq": sequence,
                "lat": latitude,
                "lon": longitude,
                "elapsed_s": _int_attr(waypoint, "elapsedTime"),
            }
        )
    fixes = [
        {
            "seq": sequence,
            "name": (fix.text or "").strip(),
            "elapsed_s": _int_attr(fix, "elapsedTime"),
        }
        for sequence, fix in _sequenced(traversal, "fix")
        if (fix.text or "").strip()
    ]
    route = _find_element(message, ("routeofflight", "newrouteofflight"))
    departure_procedure = _find_element(message, ("dp",))
    arrival_procedure = _find_element(message, ("star",))
    return {
        "waypoints": waypoints,
        "fixes": fixes,
        "sectors": _texts(traversal, "sector"),
        "centers": _texts(traversal, "center"),
        "airways": _texts(traversal, "airway"),
        "route_string": (
            route.attrib.get("legacyFormat", "") if route is not None else ""
        ),
        "etd": _time_attrs(message, "etd", "etdType"),
        "eta": _time_attrs(message, "eta", "etaType"),
        "departure_procedure": (
            departure_procedure.attrib.get("routeName", "")
            if departure_procedure is not None
            else ""
        ),
        "arrival_procedure": (
            arrival_procedure.attrib.get("routeName", "")
            if arrival_procedure is not None
            else ""
        ),
        "planned_altitude_ft": _planned_altitude(message),
        "filed_true_airspeed_kts": (
            _number(_find_text(message, ("filedtrueairspeed",))) or None
        ),
    }


def parse_tfm_xml_messages(payload_xml: str) -> list[dict]:
    """Extract normalized flight records while preserving the raw XML separately."""
    root = ET.fromstring(payload_xml)
    messages = [child for child in root.iter() if _local_name(child) == "fltdmessage"]
    if not messages:
        messages = [root]

    records: list[dict] = []
    for message in messages:
        callsign = (
            message.attrib.get("acid")
            or _find_text(message, ("aircraftid", "acid"))
            or ""
        ).strip().upper()
        if not callsign:
            continue
        latitude, longitude = _coordinates(message)
        records.append(
            {
                "flight_id": callsign,
                "gufi": _find_text(message, ("gufi",)) or "",
                "origin": message.attrib.get("depArpt")
                or _find_text(message, ("deparpt", "departurepoint"))
                or "",
                "destination": message.attrib.get("arrArpt")
                or _find_text(message, ("arrarpt", "arrivalpoint"))
                or "",
                "current_lat": latitude,
                "current_lon": longitude,
                "current_altitude_ft": _altitude(
                    _find_text(
                        message,
                        (
                            "simplealtitude",
                            "reportedaltitude",
                            "actualaltitude",
                            "altitude",
                            "alt",
                        ),
                    )
                ),
                "velocity_kts": _number(
                    _find_text(message, ("speed", "groundspeed", "spd"))
                ),
                "msg_type": message.attrib.get("msgType") or "",
                "flight_ref": message.attrib.get("flightRef") or "",
                **_aircraft(message),
                "planned_route": _planned_route(message),
                "event_time": message.attrib.get("sourceTimeStamp")
                or _find_text(
                    message,
                    ("sourcetimestamp", "timeatposition", "timestamp", "time"),
                )
                or datetime.now(UTC).isoformat(),
                "source": "FAA_SWIM_TFM_R14",
            }
        )
    return records
