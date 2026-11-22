"""Helpers shared by stub Databricks Job entry points."""

import json
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    """Write one structured result suitable for job logs."""
    # Flush immediately: serverless job processes can be torn down abruptly
    # at exit, which drops buffered stdout from the run log.
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
