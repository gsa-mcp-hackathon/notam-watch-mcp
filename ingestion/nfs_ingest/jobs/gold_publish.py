"""Publish the PostGIS gold serving layer (Lakebase) from Delta silver."""

import argparse

from nfs_ingest.jobs._output import emit
from nfs_ingest.services.gold_publisher import publish_gold


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Do not (re)apply ingestion/resources/lakebase/gold_schema.sql first.",
    )
    args = parser.parse_args()

    from pyspark.sql import SparkSession

    emit(
        publish_gold(
            SparkSession.builder.getOrCreate(), apply_schema=not args.skip_schema
        )
    )


if __name__ == "__main__":
    main()
