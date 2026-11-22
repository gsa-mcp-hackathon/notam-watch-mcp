"""Optional airport-reference resolution for NOTAM text parsing."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

AIRPORT_SUFFIXES = {"AIRPORT", "AIRFIELD", "AERODROME", "APT"}


def normalize_airport_name(value: str) -> str:
    text = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode()
        .upper()
    )
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text.replace("'", "")).split())


def resolve_airport_reference(
    reference: str,
    airport_lookup: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    lookup = airport_lookup or {}
    normalized = normalize_airport_name(reference)
    if normalized in lookup:
        return lookup[normalized]
    words = normalized.split()
    while words and words[-1] in AIRPORT_SUFFIXES:
        words.pop()
    return lookup.get(" ".join(words), [])

