"""The query SQL files load, are fully filled in and are all used."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.tool_resources.queries import common

QUERIES_DIR = Path(common.__file__).parent
SQL_NAMES = sorted(p.stem for p in (QUERIES_DIR / "sql").glob("*.sql"))
# Per-call slots (not in SQL_FRAGMENTS), with a sample value.
EXTRA_SLOTS = {"flights_approaching_hazards": {"hazard_type": "TRUE"}}


def _used_names() -> set[str]:
    text = "".join(p.read_text() for p in QUERIES_DIR.glob("*.py"))
    return set(re.findall(r'\bsql\(\s*"([a-z_]+)"', text))


def test_every_sql_file_is_used_and_every_used_name_exists() -> None:
    assert SQL_NAMES
    assert _used_names() == set(SQL_NAMES)


@pytest.mark.parametrize("name", SQL_NAMES)
def test_sql_file_renders_cleanly(name: str) -> None:
    raw = (QUERIES_DIR / "sql" / f"{name}.sql").read_text()
    assert raw.startswith("-- "), "each file starts with a comment header"
    rendered = common.sql(name, **EXTRA_SLOTS.get(name, {}))
    assert "{" not in rendered and "}" not in rendered
    # psycopg reads every % as a placeholder: only %(name)s and %% may appear.
    stray = re.sub(r"%\([a-z_]+\)s|%%", "", rendered)
    assert "%" not in stray
