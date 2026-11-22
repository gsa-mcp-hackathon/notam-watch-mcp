"""SWIM retention entry point (Bronze, positions, flights_current)."""

import argparse

from nfs_ingest.jobs._output import emit
from nfs_ingest.services.retention import run_retention


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible rows and audit them without deleting or vacuuming.",
    )
    args = parser.parse_args()

    from pyspark.sql import SparkSession

    emit(run_retention(SparkSession.builder.getOrCreate(), dry_run=args.dry_run))


if __name__ == "__main__":
    main()
