# NFS ingestion (Databricks jobs)

The data pipeline that fills the Lakebase PostGIS gold layer the MCP server
reads. It is **not part of the MCP server**: the server only queries the gold
tables. It lives in this repository for now and could become its own project.

```text
FAA NMS (NOTAMs) ---> nms_ingest ---+
FAA SWIM R14 (flights) -> swim_ingest +--> Delta tables (nfs_*)
FAA NASR (airports) --> nasr_airports_ingest     |
FAA SAA (SUA shapes) -> saa_airspace_ingest      v
                                    gold_publish (every 5 min) --> Lakebase PostGIS
                                                                  schema aviation
                                    daily_retention (hourly) trims SWIM tables
```

## Jobs

Every job runs `ingestion/run_job.py <job> [args]` from the `mcp-nfs-repo` Git
folder on serverless compute. The runner puts `ingestion/` and `src/` on the
import path and calls `nfs_ingest.jobs.<job>.main()`.

| Databricks job | Command | Schedule |
| --- | --- | --- |
| NFS NMS Incremental Ingest (5 min) | `nms_ingest --mode incremental` | every 5 min |
| NFS SWIM R14 Streaming Ingest | `swim_ingest --mode stream` | continuous |
| NFS Gold Publish (5 min) | `gold_publish` | every 5 min |
| NFS SWIM Retention (hourly) | `daily_retention --dry-run` | hourly |
| (manual) | `nasr_airports_ingest`, `saa_airspace_ingest` | each 28-day NASR cycle |

## Layout

```text
ingestion/
|-- run_job.py            entry point used by the Databricks jobs
|-- requirements.txt      job dependencies
|-- nfs_ingest/
|   |-- config.py         feed endpoints, credentials, table names (extends server.config)
|   |-- jobs/             one module per job (argument parsing, JSON result)
|   |-- services/         NMS / SWIM clients and parsers, NOTAM text parser,
|   |                     NASR / SAA readers, gold publisher, retention
|   `-- contracts/        typed data-model contracts
|-- resources/
|   |-- lakebase/         gold_schema.sql (PostGIS gold layer), init_postgis.py
|   `-- sql/              Delta schemas, migrations, aircraft reference
`-- tests/
```

Shared with the MCP server (imported from `src/server`): the base settings
class and `.env` precedence (`server.config`), the Lakebase connection
(`server.tool_resources.services.gold_db`), and the SUA timesheet helpers and
constants (`server.tool_resources.services.sua_schedule` and
`server.tool_resources.constants`). The MCP server never imports `nfs_ingest`.

## Credentials and settings

Precedence, highest first: `Settings(...)`, the `.env` file, process
environment variables, defaults. NMS/SWIM credentials still unset are read
from the Databricks secret scope `nfs` by `settings.apply_secret_scope()`
(called by the NMS and SWIM jobs). To run outside Databricks, copy
`.env.example` to `.env` and fill in the credentials; the secret scope is then
never contacted. `NFS_SECRET_SCOPE=none` turns the fallback off.

Provide authorized feed endpoints and credentials in your environment or secret
scope. Configure `SWIM_CERT_PATH` with your approved certificate directory if
the system trust store is insufficient. Credentials and certificates are not
bundled. Replace the example `users.nfs_demo` schema with your workspace schema.

## Run locally

```bash
uv sync --group dev --group ingestion
uv run python ingestion/run_job.py nms_ingest --mode probe
uv run python ingestion/run_job.py swim_ingest --mode probe
uv run python ingestion/run_job.py daily_retention --dry-run
```

NMS shadow pull that does not advance the watermark:

```bash
uv run python ingestion/run_job.py nms_ingest --mode incremental \
  --profile <databricks-profile> --warehouse-id <sql-warehouse-id> \
  --since <timezone-aware-iso-timestamp> --shadow
```

One-message SWIM R14 Bronze test (acknowledged only after the Delta row is
verified):

```bash
uv run python ingestion/run_job.py swim_ingest --mode receive-one \
  --profile <databricks-profile> --warehouse-id <sql-warehouse-id> \
  --table <catalog.schema.bronze-table> --timeout-ms 120000
```

The NMS job stores immutable raw features, lifecycle versions and accepted
hazard geometry, and advances its watermark only after all writes are
verified (see `docs/nms-watermark.md`).
