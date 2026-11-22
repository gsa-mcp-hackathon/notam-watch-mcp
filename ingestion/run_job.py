"""Run an nfs_ingest.jobs entry point from source (no wheel build required).

Used by Databricks Jobs ``spark_python_task`` with ``git_source`` (or a Git
folder) on serverless compute. The first argument selects the job module
under ``nfs_ingest.jobs``; remaining arguments are passed through, e.g.:

    python ingestion/run_job.py nms_ingest --mode incremental

Both ``ingestion/`` (the jobs) and ``src/`` (the shared Lakebase connection
and base settings in the ``server`` package) are put on the import path.
"""

import importlib
import os
import sys

JOBS = {
    "nms_ingest",
    "nasr_airports_ingest",
    "saa_airspace_ingest",
    "swim_ingest",
    "daily_retention",
    "gold_publish",
}


def _candidate_roots() -> list[str]:
    roots: list[str] = []
    if os.environ.get("NFS_REPO_ROOT"):
        roots.append(os.environ["NFS_REPO_ROOT"])
    try:
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except NameError:  # pragma: no cover - some runners exec without __file__
        pass
    if sys.argv and sys.argv[0]:
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))))
    roots.extend([os.getcwd(), os.path.dirname(os.getcwd())])
    return roots


def _import_paths() -> list[str]:
    """[ingestion/, src/] of the first candidate root that has both."""
    for root in _candidate_roots():
        ingestion = os.path.join(root, "ingestion")
        src = os.path.join(root, "src")
        if os.path.isfile(
            os.path.join(ingestion, "nfs_ingest", "__init__.py")
        ) and os.path.isfile(os.path.join(src, "server", "__init__.py")):
            return [ingestion, src]
    raise SystemExit(
        f"Cannot locate mcp-nfs ingestion/ and src/ from {_candidate_roots()}"
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        raise SystemExit(f"usage: run_job.py {{{'|'.join(sorted(JOBS))}}} [args]")
    sys.dont_write_bytecode = True
    paths = _import_paths()
    sys.path[:0] = paths
    job = sys.argv[1]
    print(f"run_job: {job} from {paths[0]} args={sys.argv[2:]}", flush=True)
    sys.argv = [f"nfs_ingest.jobs.{job}", *sys.argv[2:]]
    importlib.import_module(f"nfs_ingest.jobs.{job}").main()


if __name__ == "__main__":
    main()
