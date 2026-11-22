"""NOTAM item-D schedule parsing, using formats seen in live NMS data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from server.tool_resources.services.notam_schedule import (
    is_scheduled,
    parse_schedule,
    schedule_text,
)

# Tue 27 Oct 2026; 29 Oct is a Thursday.
START = datetime(2026, 10, 27, tzinfo=UTC)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("schedule", "moment", "expected"),
    [
        ("Daily:1230-2330~DLY 1230-2330", "2026-10-29T13:00", True),
        ("DLY 1230-2330", "2026-10-29T12:00", False),
        ("DLY 2230-0030", "2026-10-30T00:15", True),  # past midnight
        ("DLY 2230-0030", "2026-10-30T01:00", False),
        ("MON-FRI 1300-2100", "2026-10-30T14:00", True),
        ("MON-FRI 1300-2100", "2026-10-31T14:00", False),  # Saturday
        ("SAT SUN", "2026-10-31T03:00", True),
        ("SAT SUN", "2026-10-30T23:59", False),
        ("TUE WED THU FRI MON 1500-0030", "2026-10-31T00:10", True),  # Fri night
        ("29-31 0700-1500", "2026-10-30T08:00", True),
        ("29-31 0700-1500", "2026-10-28T08:00", False),
        ("30-02 1000-1100", "2026-11-01T10:30", True),  # spans month end
        ("NOV 21 1600-2359, 22 0000-1559", "2026-11-22T10:00", True),
        ("NOV 21 1600-2359, 22 0000-1559", "2026-11-21T10:00", False),
        ("OCT 30-NOV 02 0700-1500", "2026-11-01T08:00", True),
        ("MON 1000-2000, TUE-THU 0600-2000, FRI 0600-1000", "2026-10-29T07:00", True),
        ("MON 1000-2000, TUE-THU 0600-2000, FRI 0600-1000", "2026-10-30T11:00", False),
        ("DLY 1200-2200 EXC SAT SUN", "2026-10-31T13:00", False),
        ("DAILY 2200-2330 0030-0130 0400-0530", "2026-10-29T01:00", True),
        ("02 04-06 09-13 16-20 0000/0834", "2026-11-05T05:00", True),
        ("2610262130 2610270900 2610272130 2610280900", "2026-10-27T22:00", True),
        ("2610262130 2610270900 2610272130 2610280900", "2026-10-27T12:00", False),
        ("0001Z - 0400Z", "2026-10-29T03:00", True),
        ("TU-FRI 1200-1300", "2026-10-29T12:30", True),
        ("H24", "2026-10-31T13:00", True),
    ],
)
def test_schedule_evaluation(schedule: str, moment: str, expected: bool) -> None:
    parsed = parse_schedule(schedule, START)
    assert parsed.parsed, parsed.unparsed
    assert is_scheduled(parsed, at(moment)) is expected


def test_sunrise_sunset_needs_a_location() -> None:
    parsed = parse_schedule("SR-SS", START)
    assert is_scheduled(parsed, at("2026-10-29T18:00")) is None
    # Washington DC: sunrise ~11:31Z, sunset ~22:11Z on 29 Oct.
    assert is_scheduled(parsed, at("2026-10-29T18:00"), lat=38.9, lon=-77.0) is True
    assert is_scheduled(parsed, at("2026-10-29T03:00"), lat=38.9, lon=-77.0) is False
    assert is_scheduled(parse_schedule("HJ", START), at("2026-10-29T18:00"),
                        lat=38.9, lon=-77.0) is True


def test_unparsed_schedule_is_undetermined_not_false() -> None:
    parsed = parse_schedule("DLY BTN CCCM-FCCV", START)
    assert not parsed.parsed
    assert is_scheduled(parsed, at("2026-10-29T18:00")) is None


def test_no_schedule_means_always() -> None:
    assert is_scheduled(parse_schedule(None, START), at("2026-10-29T18:00")) is True


def test_schedule_text_strips_structured_prefix() -> None:
    assert schedule_text("Daily:1230-2330~DLY 1230-2330") == "DLY 1230-2330"
    assert schedule_text("29\\r\\n0000-1559") == "29 0000-1559"
