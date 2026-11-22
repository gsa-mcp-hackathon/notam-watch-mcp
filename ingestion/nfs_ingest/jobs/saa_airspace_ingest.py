"""Load FAA Special Use Airspace shapes (SAA AIXM) for one NASR cycle."""

import argparse
import collections
from datetime import date

from nfs_ingest.config import settings
from nfs_ingest.jobs._output import emit
from nfs_ingest.services.nasr_airports import airac_effective_date
from nfs_ingest.services.saa_airspace import (
    dedupe_airspaces,
    download_saa_messages,
    parse_saa_message,
    replace_cycle,
    subscription_url,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--effective-date",
        help="NASR cycle effective date YYYY-MM-DD (default: current cycle).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and parse only; do not write the table.",
    )
    args = parser.parse_args()
    effective = (
        date.fromisoformat(args.effective_date)
        if args.effective_date
        else airac_effective_date()
    )

    records, failures = [], []
    for name, payload in download_saa_messages(effective):
        try:
            records.extend(parse_saa_message(payload, source_file=name))
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures.append({"file": name, "error": str(exc)[:200]})
    parsed = len(records)
    records = dedupe_airspaces(records)
    summary = {
        "airspaces_parsed": parsed,
        "source_url": subscription_url(effective),
        "effective_date": effective.isoformat(),
        "airspaces": len(records),
        "by_type": dict(collections.Counter(r["sua_type"] for r in records)),
        "failed_files": failures[:20],
        "failed_file_count": len(failures),
    }
    if args.dry_run:
        emit({**summary, "state": "dry-run"})
        return

    from pyspark.sql import SparkSession

    written = replace_cycle(SparkSession.builder.getOrCreate(), records, effective)
    emit(
        {
            **summary,
            "table": settings.sua_airspaces_table,
            "rows_written": written,
            "state": "succeeded",
        }
    )


if __name__ == "__main__":
    main()
