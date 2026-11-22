"""SUA published times of use (FAA SAA Timesheets, local standard offsets)."""

from __future__ import annotations

from datetime import UTC, datetime

from server.tool_resources.services.sua_schedule import (
    published_times_status,
    sheet_text,
    us_dst_in_effect,
)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def sheet(**fields) -> dict:
    base = {"time_reference": "UTC", "start_date": "01-01", "end_date": "31-12",
            "day": "ANY", "start_time": "07:00", "end_time": "22:00",
            "time_offset_hr": -5.0}
    return {**base, **fields}


def active(sheets, at: str, **kw):
    return published_times_status(sheets, utc(at), **kw)["in_published_times"]


def test_us_dst_boundaries_2026() -> None:
    # 2026: starts Sun 8 Mar 02:00, ends Sun 1 Nov 02:00 daylight (01:00 std).
    assert not us_dst_in_effect(datetime(2026, 3, 8, 1, 59))
    assert us_dst_in_effect(datetime(2026, 3, 8, 2, 0))
    assert us_dst_in_effect(datetime(2026, 11, 1, 0, 59))
    assert not us_dst_in_effect(datetime(2026, 11, 1, 1, 0))


def test_day_til_is_one_continuous_session() -> None:
    # LIVE OAK MOA: WED 06:00 local to THU 02:00 local (UTC-5).
    live_oak = [sheet(day="WED", day_til="THU", start_time="06:00",
                      end_time="02:00")]
    assert active(live_oak, "2026-11-04T10:30") is False  # Wed 05:30 EST
    assert active(live_oak, "2026-11-04T12:00") is True  # Wed 07:00 EST
    assert active(live_oak, "2026-11-05T06:30") is True  # Thu 01:30 EST
    assert active(live_oak, "2026-11-05T08:00") is False  # Thu 03:00 EST
    assert active(live_oak, "2026-11-05T12:00") is False  # Thu 07:00: not a daily
    # End-of-day rule: MON 22:00 to TUE 00:00 ends exactly at midnight.
    late = [sheet(day="MON", day_til="TUE", start_time="22:00", end_time="00:00")]
    assert active(late, "2026-11-03T04:30") is True  # Mon 23:30 EST
    assert active(late, "2026-11-03T05:00") is False  # Tue 00:00 EST


def test_weekday_moa_in_local_standard_time() -> None:
    moa = [sheet(day="MON"), sheet(day="TUE"), sheet(day="FRI")]
    # Mon 2 Nov 2026 (standard time): 13:00Z = 08:00 EST.
    assert active(moa, "2026-11-02T13:00") is True
    assert active(moa, "2026-11-02T11:59") is False  # 06:59 EST
    assert active(moa, "2026-11-03T03:00") is False  # Mon 22:00 EST, end exclusive
    assert active(moa, "2026-11-07T15:00") is False  # Saturday
    status = published_times_status(moa, utc("2026-11-02T13:00"))
    assert status["evaluated_local_time"] == "Mon 2026-11-02 08:00 local (UTC-5)"
    assert status["published_times"] == [
        "MON 07:00-22:00", "TUE 07:00-22:00", "FRI 07:00-22:00"]
    assert status["times_are"] == "local, UTC-5 standard"


def test_daylight_saving_shifts_local_time() -> None:
    moa = [sheet(day="MON")]
    # Mon 6 Jul 2026 11:30Z = 07:30 EDT (06:30 EST): inside only with DST.
    assert active(moa, "2026-07-06T11:30") is True
    hawaii = [sheet(time_offset_hr=-10.0)]  # no daylight saving
    assert active(hawaii, "2026-07-06T16:30") is False  # 06:30 HST
    assert active(hawaii, "2026-07-06T17:00") is True  # 07:00 HST


def test_utc_minus_7_is_unknown_when_dst_readings_disagree() -> None:
    mountain = [sheet(time_offset_hr=-7.0)]
    # 6 Jul 13:30Z: 06:30 MST (Arizona) vs 07:30 MDT.
    status = published_times_status(mountain, utc("2026-07-06T13:30"))
    assert status["in_published_times"] is None
    assert "Arizona" in status["note"]
    assert active(mountain, "2026-07-06T18:00") is True  # inside either way
    assert active(mountain, "2026-12-07T13:30") is False  # winter: 06:30 MST
    # Unknown for another reason (WORK_DAY): no misleading daylight-saving note.
    work_day = published_times_status(
        [sheet(day="WORK_DAY", time_offset_hr=-7.0)], utc("2026-07-06T18:00")
    )
    assert work_day["in_published_times"] is None
    assert "Arizona" not in (work_day["note"] or "")


def test_overnight_span_and_midnight_end() -> None:
    corsair = [sheet(day="MON", start_time="08:00", end_time="01:00")]
    assert active(corsair, "2026-11-03T05:30") is True  # Tue 00:30 EST
    assert active(corsair, "2026-11-03T07:00") is False  # Tue 02:00 EST
    bulldog = [sheet(start_time="07:00", end_time="00:00")]
    assert active(bulldog, "2026-11-03T04:30") is True  # 23:30 EST


def test_seasonal_range_wraps_the_year() -> None:
    winter = [sheet(start_date="01-09", end_date="30-04")]  # Sep 1 - Apr 30
    assert active(winter, "2026-01-15T15:00") is True
    assert active(winter, "2026-06-15T15:00") is False
    assert sheet_text(winter[0]) == "ANY 07:00-22:00 (09-01 to 04-30)"


def test_sunrise_sunset_needs_location() -> None:
    daylight = [sheet(start_time=None, end_time=None, start_event="SR",
                      end_event="SS", time_offset_hr=-7.0)]
    # Colorado Springs, 7 Dec 2026 (no DST): local noon vs local midnight.
    kw = {"lat": 38.8, "lon": -104.8}
    assert active(daylight, "2026-12-07T19:00", **kw) is True
    assert active(daylight, "2026-12-08T07:00", **kw) is False
    assert active(daylight, "2026-12-07T19:00") is None


def test_unknown_and_excluded_sheets() -> None:
    assert active([sheet(day="WORK_DAY")], "2026-11-02T15:00") is None
    except_sat = [sheet(), sheet(day="SAT", excluded="YES")]
    assert active(except_sat, "2026-11-07T15:00") is False  # Saturday excluded
    assert active(except_sat, "2026-11-06T15:00") is True
    none = published_times_status([], utc("2026-11-02T15:00"))
    assert none["in_published_times"] is None and "NOTAM activation" in none["note"]
    assert none.keys() == published_times_status(except_sat, utc(
        "2026-11-06T15:00")).keys()  # same shape either way


def test_stored_json_and_issue_notes() -> None:
    stored = (
        '[{"day": "ANY", "start_time": "07:00", "end_time": "01:00", '
        '"time_offset_hr": -5.0, '
        '"issues": ["local_time_offset", "overnight_span"]}]'
    )
    status = published_times_status(stored, utc("2026-11-03T05:30"))
    assert status["in_published_times"] is True
    assert status["note"] == "Timesheet anomalies: overnight_span."
