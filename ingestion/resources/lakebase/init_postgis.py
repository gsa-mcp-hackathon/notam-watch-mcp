# Databricks notebook source
import json
import os

import psycopg
from databricks.sdk import WorkspaceClient
from google.protobuf.duration_pb2 import Duration

PROJECT_ID = "nfs-aviation-postgis"
BRANCH_ID = "production"
ENDPOINT_ID = "primary"
DATABASE = "databricks_postgres"
HOST = os.getenv("PGHOST") or os.environ["LAKEBASE_HOST"]

endpoint = (
    f"projects/{PROJECT_ID}/branches/{BRANCH_ID}/endpoints/{ENDPOINT_ID}"
)
workspace = WorkspaceClient()
user = workspace.current_user.me().user_name
credential = workspace.postgres.generate_database_credential(
    endpoint=endpoint,
    ttl=Duration(seconds=600),
)

with psycopg.connect(
    host=HOST,
    port=5432,
    dbname=DATABASE,
    user=user,
    password=credential.token,
    sslmode="require",
    connect_timeout=30,
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS aviation")
        cursor.execute(
            "SELECT PostGIS_Version(), "
            "ST_AsText(ST_SetSRID(ST_Point(-77.0365, 38.8977), 4326))"
        )
        postgis_version, test_geometry = cursor.fetchone()

dbutils.notebook.exit(  # noqa: F821
    json.dumps(
        {
            "connected": True,
            "project_id": PROJECT_ID,
            "branch_id": BRANCH_ID,
            "endpoint_id": ENDPOINT_ID,
            "database": DATABASE,
            "schema": "aviation",
            "postgis_version": postgis_version,
            "test_geometry": test_geometry,
        }
    )
)
