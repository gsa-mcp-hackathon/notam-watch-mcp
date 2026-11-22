"""FAA NMS connection, baseline, and incremental Databricks ingestion entry point."""

import argparse
from datetime import datetime

from nfs_ingest.config import settings
from nfs_ingest.jobs._output import emit
from nfs_ingest.services.nasr_airports import NasrAirportStore
from nfs_ingest.services.nms import (
    ingest_incremental,
    ingest_initial,
    probe_connection,
)
from nfs_ingest.services.nms_storage import NmsDatabricksWriter, NmsSqlWatermarkStore


def _airport_code_lookup(profile, warehouse_id) -> dict:
    """NASR airport lookup for location anchors; empty if not loaded yet."""
    try:
        return NasrAirportStore(
            profile=profile,
            warehouse_id=warehouse_id,
        ).load_code_lookup()
    except Exception as exc:  # noqa: BLE001 - anchors degrade gracefully
        print(f"NASR airport lookup unavailable; continuing without it: {exc}")
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("initial", "incremental", "probe"),
        default="incremental",
    )
    parser.add_argument("--probe", action="store_true")
    parser.add_argument(
        "--classifications",
        help="Comma separated baseline classifications (default: settings).",
    )
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--profile")
    parser.add_argument("--warehouse-id")
    parser.add_argument("--since")
    parser.add_argument("--shadow", action="store_true")
    args = parser.parse_args()

    # Fill NMS credentials from the Databricks secret scope when not in env.
    settings.apply_secret_scope()

    if args.probe or args.mode == "probe":
        emit(probe_connection())
        return

    writer = NmsDatabricksWriter(
        profile=args.profile,
        warehouse_id=args.warehouse_id,
    )
    watermark_store = NmsSqlWatermarkStore(
        profile=args.profile,
        warehouse_id=args.warehouse_id,
    )
    watermark_store.ensure_table()
    airport_code_lookup = _airport_code_lookup(args.profile, args.warehouse_id)
    if args.mode == "initial":
        emit(
            ingest_initial(
                writer=writer,
                watermark_store=watermark_store,
                classifications=(
                    args.classifications.split(",")
                    if args.classifications
                    else None
                ),
                advance_watermark=not args.shadow,
                batch_size=args.batch_size,
                airport_code_lookup=airport_code_lookup,
            )
        )
        return
    emit(
        ingest_incremental(
            writer=writer,
            watermark_store=watermark_store,
            since=datetime.fromisoformat(args.since) if args.since else None,
            advance_watermark=not args.shadow,
            airport_code_lookup=airport_code_lookup,
        )
    )


if __name__ == "__main__":
    main()
