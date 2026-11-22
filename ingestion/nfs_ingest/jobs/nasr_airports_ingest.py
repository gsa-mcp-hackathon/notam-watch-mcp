"""Load the FAA NASR APT_BASE airport reference table for one AIRAC cycle."""

import argparse
from datetime import UTC, date, datetime

from nfs_ingest.jobs._output import emit
from nfs_ingest.services.nasr_airports import (
    NasrAirportStore,
    airac_effective_date,
    apt_csv_url,
    download_apt_archive,
    parse_apt_base,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--effective-date",
        help="NASR cycle effective date YYYY-MM-DD (default: current cycle).",
    )
    parser.add_argument("--profile")
    parser.add_argument("--warehouse-id")
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
    loaded_at = datetime.now(UTC)
    rows = parse_apt_base(download_apt_archive(effective), loaded_at=loaded_at)
    summary = {
        "source_url": apt_csv_url(effective),
        "requested_effective_date": effective.isoformat(),
        "rows_parsed": len(rows),
        "rows_with_coordinates": sum(
            1 for row in rows if row["latitude"] is not None
        ),
    }
    if args.dry_run:
        emit({**summary, "state": "dry-run"})
        return

    store = NasrAirportStore(profile=args.profile, warehouse_id=args.warehouse_id)
    store.ensure_table()
    emit({**summary, **store.replace_cycle(rows), "state": "succeeded"})


if __name__ == "__main__":
    main()
