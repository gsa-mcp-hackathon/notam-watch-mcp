"""Static checks for the gold publisher (no Spark or Postgres needed)."""

from __future__ import annotations

import re

from nfs_ingest.services.gold_publisher import SCHEMA_SQL, gold_tables, split_statements


def test_schema_file_splits_into_statements() -> None:
    statements = split_statements(SCHEMA_SQL.read_text())
    assert statements[0].startswith("CREATE EXTENSION IF NOT EXISTS postgis")
    assert all(not s.endswith(";") for s in statements)
    assert all(not s.lstrip().startswith("--") for s in statements)
    created = {
        m.group(1)
        for s in statements
        if (m := re.match(r"CREATE TABLE IF NOT EXISTS aviation\.(\w+)", s))
    }
    assert {t.name for t in gold_tables()} <= created
    assert "publish_log" in created


def test_split_handles_comments_and_blank_chunks() -> None:
    sql = "-- header\nCREATE SCHEMA a;\n-- statement\n\n-- statement\nSELECT 1;\n"
    assert split_statements(sql) == ["CREATE SCHEMA a", "SELECT 1"]


def test_each_gold_table_loads_from_its_staging_table() -> None:
    for table in gold_tables():
        names = table.column_names
        assert len(names) == len(set(names)), table.name
        assert f"FROM {table.staging_table}" in table.insert_sql
        assert f"INSERT INTO aviation.{table.name}" in table.insert_sql
        assert "%(published_at)s" in table.insert_sql
        # psycopg treats bare % as a placeholder; only named params allowed.
        assert not re.search(r"%(?!\(published_at\)s)", table.insert_sql)


def test_hazard_classes_cover_feature_and_airspace_notams() -> None:
    (hazards,) = [t for t in gold_tables() if t.name == "notam_hazards"]
    for cls in ("'obstacle'", "'airspace'", "'navaid'", "'aerodrome'"):
        assert cls in hazards.source_sql
    assert "hazard_class" in hazards.column_names
