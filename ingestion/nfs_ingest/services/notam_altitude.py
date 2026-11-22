"""Altitude normalization shared by NOTAM enrichment stages."""

from __future__ import annotations

import math
from typing import Any


def normalize_reference(reference: Any) -> str:
    value = str(reference or "UNKNOWN").strip().upper()
    return {
        "AMSL": "MSL",
        "SFC": "AGL",
        "GND": "AGL",
        "FL": "STD",
        "UNL": "UNBOUNDED",
        "UNLIMITED": "UNBOUNDED",
    }.get(value, value)


def altitude_bound(
    value: Any,
    unit: Any = None,
    reference: Any = None,
) -> tuple[float | None, str]:
    text = str(value if value is not None else "").strip().upper()
    if text in {"UNL", "UNLIMITED"}:
        return None, "UNBOUNDED"
    if text in {"SFC", "GND"}:
        return 0.0, "AGL"
    normalized_unit = str(unit or "FT").strip().upper()
    normalized_unit = {
        "FEET": "FT",
        "FOOT": "FT",
        "METERS": "M",
        "METER": "M",
        "METRES": "M",
        "METRE": "M",
        "FLIGHT LEVEL": "FL",
        "FLIGHT LEVELS": "FL",
    }.get(normalized_unit, normalized_unit)
    normalized_reference = normalize_reference(reference)
    if text.startswith("FL"):
        text, normalized_unit = text[2:].strip(), "FL"
    if normalized_unit == "FL":
        normalized_reference = "STD"
    factors = {"FT": 1.0, "F": 1.0, "M": 1 / 0.3048, "FL": 100.0}
    try:
        number = float(text) * factors[normalized_unit]
        if math.isfinite(number):
            return number, normalized_reference
    except (ValueError, KeyError):
        pass
    return None, "UNKNOWN"


def normalize_vertical_limits(limits: Any) -> dict[str, float | str | None]:
    source = limits if isinstance(limits, dict) else {}
    result: dict[str, float | str | None] = {}
    for side in ("lower", "upper"):
        bound = source.get(side) or {}
        if not isinstance(bound, dict):
            bound = {"value": bound}
        reference = normalize_reference(
            source.get(f"{side}_reference", bound.get("reference"))
        )
        value, normalized_reference = altitude_bound(
            source.get(f"{side}_ft", bound.get("value")),
            "FT" if f"{side}_ft" in source else bound.get("unit"),
            reference,
        )
        if reference == "UNBOUNDED":
            value, normalized_reference = None, "UNBOUNDED"
        result[f"{side}_ft"] = value
        result[f"{side}_reference"] = normalized_reference
    return result


def inverted_vertical_limits(limits: dict[str, Any]) -> bool:
    return bool(
        limits["lower_reference"] == limits["upper_reference"]
        and limits["lower_ft"] is not None
        and limits["upper_ft"] is not None
        and limits["lower_ft"] > limits["upper_ft"]
    )


def merge_vertical_limits(
    existing: Any,
    candidate: Any,
) -> tuple[dict[str, float | str | None], list[str]]:
    result = normalize_vertical_limits(existing)
    proposed = normalize_vertical_limits(candidate)
    warnings: list[str] = []
    if inverted_vertical_limits(result):
        result = normalize_vertical_limits({})
        warnings.append("Invalid stored altitude interval discarded")
    if inverted_vertical_limits(proposed):
        return result, warnings + ["Inverted text altitude interval ignored"]
    for side in ("lower", "upper"):
        reference_key = f"{side}_reference"
        value_key = f"{side}_ft"
        current_value = result[value_key]
        proposed_value = proposed[value_key]
        if proposed_value is None and proposed[reference_key] != "UNBOUNDED":
            continue
        if result[reference_key] == "UNKNOWN" or (
            current_value is None and result[reference_key] != "UNBOUNDED"
        ):
            result[value_key] = proposed_value
            result[reference_key] = proposed[reference_key]
        elif (
            result[reference_key] != proposed[reference_key]
            or (current_value is None) != (proposed_value is None)
            or (
                current_value is not None
                and proposed_value is not None
                and not math.isclose(current_value, proposed_value, abs_tol=0.01)
            )
        ):
            warnings.append(f"Existing {side} bound preserved over conflicting text")
    if inverted_vertical_limits(result):
        return normalize_vertical_limits(existing), warnings + [
            "Text bounds would invert the stored interval; ignored"
        ]
    return result, warnings

