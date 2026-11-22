"""SWIM R14 probe, one-message test, and streaming ingestion entry point."""

import argparse

from nfs_ingest.config import settings
from nfs_ingest.jobs._output import emit
from nfs_ingest.services.swim import consume_one_message, probe_connection
from nfs_ingest.services.swim_storage import SwimBronzeWriter


def _stream(args: argparse.Namespace) -> None:
    from pyspark.sql import SparkSession

    from nfs_ingest.services.swim_pipeline import SwimSparkWriter, stream_messages

    writer = SwimSparkWriter(SparkSession.builder.getOrCreate())
    writer.ensure_tables()
    emit(
        stream_messages(
            writer.write,
            duration_seconds=args.duration_seconds
            or settings.swim_stream_duration_seconds,
            flush_max_messages=args.flush_max_messages
            or settings.swim_flush_max_messages,
            flush_max_seconds=args.flush_max_seconds
            or settings.swim_flush_max_seconds,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("probe", "receive-one", "stream"), default="probe"
    )
    parser.add_argument("--profile")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--table")
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--flush-max-messages", type=int)
    parser.add_argument("--flush-max-seconds", type=int)
    args = parser.parse_args()

    # Fill SWIM credentials from the Databricks secret scope when not in env.
    settings.apply_secret_scope()

    if args.mode == "stream":
        _stream(args)
        return

    if args.mode == "probe":
        emit(probe_connection())
        return

    writer = SwimBronzeWriter(
        profile=args.profile,
        warehouse_id=args.warehouse_id,
        table_name=args.table,
    )
    writer.ensure_table()
    emit(consume_one_message(writer.write, timeout_ms=args.timeout_ms))


if __name__ == "__main__":
    main()
