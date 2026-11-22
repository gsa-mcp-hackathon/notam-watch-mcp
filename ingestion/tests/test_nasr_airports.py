"""Tests for FAA NASR APT_BASE parsing and NOTAM airport-code lookup."""

import io
import json
import zipfile
from datetime import UTC, date, datetime

import pytest

from nfs_ingest.services.nasr_airports import (
    airac_effective_date,
    apt_csv_url,
    build_airport_code_lookup,
    parse_apt_base,
)

NOW = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)

HEADER = (
    "EFF_DATE,SITE_NO,SITE_TYPE_CODE,STATE_CODE,ARPT_ID,CITY,COUNTRY_CODE,"
    "ARPT_NAME,OWNERSHIP_TYPE_CODE,FACILITY_USE_CODE,LAT_DECIMAL,LONG_DECIMAL,"
    "ELEV,RESP_ARTCC_ID,ARPT_STATUS,ICAO_ID"
)


def archive(*rows: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("APT_BASE.csv", "\n".join((HEADER, *rows)))
    return buffer.getvalue()


def test_airac_cycle_effective_dates() -> None:
    assert airac_effective_date(date(2026, 10, 28)) == date(2026, 10, 1)
    assert airac_effective_date(date(2026, 10, 29)) == date(2026, 10, 29)


def test_apt_csv_url_uses_nasr_extra_naming() -> None:
    assert apt_csv_url(date(2026, 10, 1)).endswith(
        "/28DaySub/extra/01_Oct_2026_APT_CSV.zip"
    )


def test_parse_apt_base_normalizes_rows() -> None:
    rows = parse_apt_base(
        archive(
            "2026/10/01,00128.*A,A,NY,JFK,NEW YORK,US,JOHN F KENNEDY INTL,PU,PU,"
            "40.63992500,-73.77869278,13,ZNY,O,KJFK",
            "2026/10/01,01234.*A,A,AL,0J0,ABBEVILLE,US,ABBEVILLE MUNI,PU,PU,"
            "31.60172027,-85.23854472,468.3,ZJX,O,",
        ),
        loaded_at=NOW,
    )

    assert len(rows) == 2
    assert rows[0]["arpt_id"] == "JFK"
    assert rows[0]["icao_id"] == "KJFK"
    assert rows[0]["latitude"] == pytest.approx(40.639925)
    assert rows[0]["nasr_effective_date"] == "2026-10-01"
    assert rows[1]["icao_id"] is None
    assert rows[1]["elevation_ft"] == 468.3


def test_lookup_indexes_faa_and_icao_codes() -> None:
    rows = parse_apt_base(
        archive(
            "2026/10/01,00128.*A,A,NY,JFK,NEW YORK,US,JOHN F KENNEDY INTL,PU,PU,"
            "40.63992500,-73.77869278,13,ZNY,O,KJFK",
        ),
        loaded_at=NOW,
    )

    lookup = build_airport_code_lookup(rows)

    assert lookup["JFK"] == lookup["KJFK"]
    assert lookup["JFK"][0]["longitude"] == pytest.approx(-73.77869278)


def test_json_chunks_respect_size_and_count_limits() -> None:
    from nfs_ingest.services.nasr_airports import _json_chunks

    rows = [{"site_no": str(i), "arpt_id": f"A{i}"} for i in range(10)]

    by_count = list(_json_chunks(rows, batch_size=4))
    by_size = list(_json_chunks(rows, batch_size=100, max_chars=80))

    assert [len(json.loads(chunk)) for chunk in by_count] == [4, 4, 2]
    assert all(len(chunk) <= 80 for chunk in by_size)
    assert sum(len(json.loads(chunk)) for chunk in by_size) == 10


def test_parse_rejects_archive_without_apt_base() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("APT_RWY.csv", "x")

    with pytest.raises(ValueError, match="APT_BASE"):
        parse_apt_base(buffer.getvalue(), loaded_at=NOW)
