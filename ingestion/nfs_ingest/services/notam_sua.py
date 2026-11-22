"""Link NOTAMs to the Special Use Airspace they name.

SUA activation NOTAMs carry no coordinates, only the airspace's name:

    !SUAC 10/483 ZAB AIRSPACE R5107K ACT SFC-FL300
    !SUAW 10/237 ZOA AIRSPACE FALLON NORTH 2 MOA ACT 100FT UP TO BUT NOT
        INCLUDING FL180
    ...TEMPORARY FLIGHT RESTRICTIONS ARE IN EFFECT WI THE AREA DESCRIBED AS R3807

This module finds those references (restricted/prohibited/warning/alert
areas by designator, MOAs and NSAs by name), and the activated altitude band
when the NOTAM states one. Only designators that exist in the FAA SAA table
are accepted, so VOR radials such as ``HCM VORTAC R-082`` never match, and
only airspace-type NOTAM text is scanned.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Text must look like an airspace NOTAM to be scanned at all.
_AIRSPACE_CONTEXT = re.compile(
    r"^!SUA|\bAIRSPACE\b|\bRESTRICTED AREA\b|\bWARNING AREA\b|\bMOA\b"
)
# R-2508, R2503A, R5107A/B/C, R6714 A/F/G/H, W-570B, P-56B, A-291C
_DESIGNATOR = re.compile(
    r"(?<![A-Z0-9/])([RPWA])-?(\d{2,4})"
    r"(\s[A-Z]{1,2}(?:/[A-Z]{1,2})+|[A-Z]{0,2}(?:/[A-Z]{1,2})*)(?![A-Z0-9])"
)
_NAVAID_BEFORE = re.compile(r"(VOR|VORTAC|TACAN|VOR/DME|NDB|DME|RADIAL)\W*$")
# A reference counts only when the NOTAM is about that airspace itself:
# "<ref> ACT ..." (activation) or a boundary such as "AREA DESCRIBED AS <ref>"
# / "WI <ref>". Lists of areas, exclusions ("EXCLUDE ... R-2206A") and
# passing mentions are ignored.
_ACT_AFTER = re.compile(r"^\s*(?:\([A-Z]+\)\s*)?ACT\b")
_BOUNDARY_BEFORE = re.compile(
    r"(?:(?:DESCRIBED|DEFINED)\s+AS|\bWI|\bWITHIN)\s+(?:THE\s+)?"
    r"(?:LATERAL\s+(?:LIMITS|BOUNDARIES)\s+OF\s+(?:THE\s+)?)?$"
)
_EXCLUSION_BEFORE = re.compile(r"\b(EXC|EXCEPT|EXCLUD\w*|OUTSIDE)\b")


def _is_subject(normalized: str, start: int, end: int) -> bool:
    before = normalized[max(0, start - 80) : start]
    if _EXCLUSION_BEFORE.search(before[-40:]):
        return False
    return bool(
        _ACT_AFTER.match(normalized[end : end + 20])
        or _BOUNDARY_BEFORE.search(before)
    )

_BOUND = (
    r"(SFC|GND|UNL|FL\s?\d{2,3}"
    r"|\d{1,3}(?:,\d{3})+(?:\s?FT)?(?:\s?(?:AGL|MSL))?"
    r"|\d{2,5}\s?FT(?:\s?(?:AGL|MSL))?)"
)
_BAND = re.compile(
    rf"\bACT\s+{_BOUND}\s*"
    r"(?:-|\s+TO\s+|\s+(UP\s+TO\s+BUT\s+NOT\s+INCLUDING)\s+)\s*"
    rf"{_BOUND}"
)


@dataclass(frozen=True)
class SuaIndex:
    """Lookup keys built from the SUA reference table."""

    designators: frozenset[str]
    names: dict[str, str]  # 'LIVE OAK MOA' -> designator

    @classmethod
    def build(cls, rows: Iterable[dict[str, Any]]) -> SuaIndex:
        designators, names = set(), {}
        for row in rows:
            designator = row.get("designator_normalized") or ""
            if not designator:
                continue
            if (row.get("sua_type") or "") in ("MOA", "NSA"):
                key = _normalize(row.get("name_code") or "")
                if key:
                    names[key] = designator
            else:
                designators.add(designator)
        return cls(frozenset(designators), names)


def is_candidate(text: str | None) -> bool:
    return bool(text) and bool(_AIRSPACE_CONTEXT.search(text.upper()))


def _normalize(text: str) -> str:
    """Upper-case, drop apostrophes (O'NEILL -> ONEILL), punctuation to space."""
    text = text.upper().replace("'", "").replace("&APOS;", "")
    return " ".join(re.sub(r"[^A-Z0-9/\-]", " ", text).split())


def _bound(value: str) -> tuple[float | None, str]:
    value = value.replace(" ", "").replace(",", "")
    if value in ("SFC", "GND"):
        return 0.0, "AGL"
    if value == "UNL":
        return None, "UNBOUNDED"
    if value.startswith("FL"):
        return float(value[2:]) * 100, "STD"
    reference = "AGL" if value.endswith("AGL") else "MSL"
    number = re.match(r"\d+", value)
    return (float(number.group()) if number else None), reference


def activation_band(text: str) -> dict[str, Any] | None:
    """Altitude band stated after ACT, e.g. 'ACT SFC-FL300'."""
    match = _BAND.search(text.upper())
    if not match:
        return None
    lower_ft, lower_ref = _bound(match.group(1))
    upper_ft, upper_ref = _bound(match.group(3))
    if match.group(2) and upper_ft is not None:
        upper_ft -= 100  # "up to but not including FL180"
    return {
        "lower_ft": lower_ft,
        "lower_reference": lower_ref,
        "upper_ft": upper_ft,
        "upper_reference": upper_ref,
    }


def find_sua_references(text: str | None, index: SuaIndex) -> list[dict[str, str]]:
    """SUA airspaces named in a NOTAM (designator + how it matched)."""
    if not is_candidate(text):
        return []
    normalized = _normalize(text or "")
    found: dict[str, dict[str, str]] = {}
    for match in _DESIGNATOR.finditer(normalized):
        if _NAVAID_BEFORE.search(normalized[: match.start()]):
            continue
        if not _is_subject(normalized, match.start(), match.end()):
            continue
        prefix, number, suffix = match.groups()
        base = f"{prefix}{number}"
        letters = [p for p in suffix.strip().split("/") if p] if suffix else []
        # R5107A/B/C -> R5107A, R5107B, R5107C
        wanted = [f"{base}{letter}" for letter in letters] or [base]
        for designator in wanted:
            if designator in index.designators:
                found.setdefault(
                    designator,
                    {"designator": designator, "match_type": "designator",
                     "matched_text": match.group()},
                )
            elif not letters:
                # 'R2503' names the whole complex R2503A, R2503B, ...
                for candidate in sorted(index.designators):
                    if re.fullmatch(rf"{base}[A-Z]{{1,2}}", candidate):
                        found.setdefault(
                            candidate,
                            {"designator": candidate,
                             "match_type": "designator_group",
                             "matched_text": match.group()},
                        )
    padded = f" {normalized} "
    hits = []
    for name in index.names:
        position = padded.find(f" {name} ")
        if position >= 0 and _is_subject(
            padded, position + 1, position + 1 + len(name)
        ):
            hits.append(name)
    for name in hits:
        # Prefer 'FALLON NORTH 2 MOA' over a shorter name it contains.
        if any(other != name and f" {name} " in f" {other} " for other in hits):
            continue
        designator = index.names[name]
        found.setdefault(
            designator,
            {"designator": designator, "match_type": "name", "matched_text": name},
        )
    return list(found.values())


def link_notams(
    notams: Iterable[dict[str, Any]], index: SuaIndex
) -> list[dict[str, Any]]:
    """Link rows for NOTAMs (notam_version_id, raw_text) naming an SUA."""
    links = []
    for notam in notams:
        text = notam.get("raw_text") or ""
        references = find_sua_references(text, index)
        if not references:
            continue
        band = activation_band(text)
        for reference in references:
            links.append(
                {
                    "notam_version_id": notam["notam_version_id"],
                    **reference,
                    "lower_ft": band["lower_ft"] if band else None,
                    "lower_reference": band["lower_reference"] if band else None,
                    "upper_ft": band["upper_ft"] if band else None,
                    "upper_reference": band["upper_reference"] if band else None,
                    "altitude_source": "notam_text" if band else "sua_published",
                }
            )
    return links
