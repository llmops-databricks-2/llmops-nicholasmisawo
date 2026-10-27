"""Keep a Lakebase (Postgres) copy of the searchable chunks, for semantic and hybrid search.

A Lakebase synced table copies a Unity Catalog Delta table into Postgres and follows its changes
(through the Delta table's Change Data Feed). Ours copies the dbt knowledge base, fcr_knowledge_base, with:

- source_key as the primary key
- embedding as a native pgvector vector(1024) column, so Postgres can search it directly
- TRIGGERED scheduling: it refreshes when a pipeline run asks, not continuously (cheaper)

Each run creates the synced table the first time (which copies every row) or refreshes it (which
copies only what changed). Either way, a failed sync fails the run.

One-time setup: the vector column needs the pgvector extension in the Lakebase database. Every
environment shares that database, so install it once, from your machine, with ensure_vector_extension.
"""

import time
from typing import TYPE_CHECKING

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.pipelines import UpdateInfoState
from databricks.sdk.service.postgres import (
    NewPipelineSpec,
    SyncedTable,
    SyncedTableState,
    SyncedTableSyncedTableSpec,
    SyncedTableSyncedTableSpecPgSpecificType,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy,
    SyncedTableSyncedTableSpecTypeOverride,
)
from loguru import logger

from fcr_curator.config import ProjectConfig
from fcr_curator.tables import KNOWLEDGE_BASE_TABLE

if TYPE_CHECKING:
    import psycopg

POSTGRES_DATABASE = "databricks_postgres"  # The database every Lakebase project starts with
LAKEBASE_BRANCH = "production"
LAKEBASE_ENDPOINT = "primary"  # The read-write compute every project starts with
EMBEDDING_DIMENSIONS = 1024  # course_ops.gateway.embed (gte-large-en-v1.5)
REFRESH_TIMEOUT_MINUTES = 60
REFRESH_POLL_SECONDS = 15


# --- Names and settings -----------------------------------------------------------------------


def synced_table_name(config: ProjectConfig, env: str) -> str:
    """Return the synced table's name: <catalog>.<schema>.fcr_knowledge_base_lakebase_<env>.

    It also names the Postgres table: fcr_knowledge_base_lakebase_<env> in schema <schema>. "lakebase"
    tells it apart from the Delta table it copies, next to which it sorts in Catalog Explorer. Every
    environment shares one Lakebase database, so the environment is in the name to keep dev, acc and
    prd apart.
    """
    return f"{config.full_schema_name}.{KNOWLEDGE_BASE_TABLE}_lakebase_{env}"


def endpoint_path(config: ProjectConfig) -> str:
    """Return the resource path of the project's read-write Postgres compute."""
    return f"projects/{config.lakebase_instance}/branches/{LAKEBASE_BRANCH}/endpoints/{LAKEBASE_ENDPOINT}"


def synced_table_spec(config: ProjectConfig, source_table: str) -> SyncedTableSyncedTableSpec:
    """Return how to sync source_table: keyed by source_key, embedding as vector(1024), refreshed on request."""
    return SyncedTableSyncedTableSpec(
        source_table_full_name=source_table,
        primary_key_columns=["source_key"],
        scheduling_policy=SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.TRIGGERED,
        branch=f"projects/{config.lakebase_instance}/branches/{LAKEBASE_BRANCH}",
        postgres_database=POSTGRES_DATABASE,
        create_database_objects_if_missing=True,  # Creates the Postgres schema the first time
        type_overrides=[
            SyncedTableSyncedTableSpecTypeOverride(
                column_name="embedding",
                pg_type=SyncedTableSyncedTableSpecPgSpecificType.PG_SPECIFIC_TYPE_VECTOR,
                size=EMBEDDING_DIMENSIONS,
            )
        ],
        new_pipeline_spec=NewPipelineSpec(budget_policy_id=config.usage_policy_id),
    )


# --- Postgres ---------------------------------------------------------------------------------


def connect(workspace: WorkspaceClient, config: ProjectConfig) -> "psycopg.Connection":
    """Open a connection to the Lakebase database as the current Databricks user or service principal.

    The password is a short-lived OAuth token (about an hour), so open a connection when it's needed.
    """
    # Imported here, not at the top: loading psycopg's compiled library aborts the Python process on
    # serverless job compute, and the sync itself only needs the Databricks SDK
    import psycopg

    endpoint = endpoint_path(config)
    return psycopg.connect(
        host=workspace.postgres.get_endpoint(endpoint).status.hosts.host,
        dbname=POSTGRES_DATABASE,
        user=workspace.current_user.me().user_name,  # A service principal's user name is its application id
        password=workspace.postgres.generate_database_credential(endpoint=endpoint).token,
        sslmode="require",
    )


def ensure_vector_extension(workspace: WorkspaceClient, config: ProjectConfig) -> None:
    """Install pgvector in the Lakebase database, which a vector column needs; does nothing if installed."""
    with connect(workspace, config) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")


# --- The synced table -------------------------------------------------------------------------


def _existing_synced_table(workspace: WorkspaceClient, name: str) -> SyncedTable | None:
    try:
        return workspace.postgres.get_synced_table(f"synced_tables/{name}")
    except NotFound:
        return None


def _wait_for_refresh(workspace: WorkspaceClient, pipeline_id: str, update_id: str) -> None:
    deadline = time.monotonic() + REFRESH_TIMEOUT_MINUTES * 60
    while time.monotonic() < deadline:
        state = workspace.pipelines.get_update(pipeline_id, update_id).update.state
        if state == UpdateInfoState.COMPLETED:
            return
        if state in (UpdateInfoState.FAILED, UpdateInfoState.CANCELED):
            raise RuntimeError(f"Lakebase sync {state.value}; see pipeline {pipeline_id}, update {update_id}")
        time.sleep(REFRESH_POLL_SECONDS)
    raise TimeoutError(f"Lakebase sync still running after {REFRESH_TIMEOUT_MINUTES} minutes (pipeline {pipeline_id})")


def sync_to_lakebase(workspace: WorkspaceClient, name: str, spec: SyncedTableSyncedTableSpec) -> str:
    """Create the synced table if it's missing, else refresh it and wait; return what was done.

    Raises RuntimeError if the sync fails, so the job run fails rather than reporting success.
    """
    synced = _existing_synced_table(workspace, name)
    if synced is None:
        logger.info(f"Creating synced table {name} from {spec.source_table_full_name}")
        created = workspace.postgres.create_synced_table(SyncedTable(spec=spec), synced_table_id=name).wait()
        if created.status.detailed_state == SyncedTableState.SYNCED_TABLE_OFFLINE_FAILED:
            raise RuntimeError(f"Creating synced table {name} failed: {created.status.message}")
        return "created"

    pipeline_id = synced.status.pipeline_id
    logger.info(f"Refreshing synced table {name} (pipeline {pipeline_id}, state {synced.status.detailed_state})")
    update_id = workspace.pipelines.start_update(pipeline_id).update_id
    _wait_for_refresh(workspace, pipeline_id, update_id)
    return "refreshed"
