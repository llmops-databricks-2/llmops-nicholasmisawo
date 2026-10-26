"""Tests for keeping a Lakebase synced table of the searchable chunks."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from databricks.sdk.errors import NotFound
from databricks.sdk.service.pipelines import UpdateInfoState
from databricks.sdk.service.postgres import (
    SyncedTableState,
    SyncedTableSyncedTableSpecPgSpecificType,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy,
)

from fcr_curator.config import ProjectConfig
from fcr_curator.lakebase import endpoint_path, sync_to_lakebase, synced_table_name, synced_table_spec

CONFIG = ProjectConfig.from_yaml(str(Path(__file__).resolve().parents[1] / "project_config.yml"), "dev")
SOURCE = "llmops_dev.nicholasmisawo.gold_fcr_chunks_by_section"


class FakeWorkspace:
    """Records synced-table and pipeline calls.

    The synced table exists only if given a pipeline id; creating one ends in created_state, and a
    refresh ends in refresh_state.
    """

    def __init__(
        self,
        existing_pipeline_id: str | None = None,
        created_state: SyncedTableState = SyncedTableState.SYNCED_TABLE_ONLINE,
        refresh_state: UpdateInfoState = UpdateInfoState.COMPLETED,
    ) -> None:
        self.created: list[str] = []
        self.refreshed: list[str] = []

        def get_synced_table(name: str) -> SimpleNamespace:
            if existing_pipeline_id is None:
                raise NotFound(f"{name} not found")
            return SimpleNamespace(status=SimpleNamespace(pipeline_id=existing_pipeline_id, detailed_state="ONLINE"))

        def create_synced_table(synced_table: object, synced_table_id: str) -> SimpleNamespace:
            self.created.append(synced_table_id)
            status = SimpleNamespace(detailed_state=created_state, message="extension missing")
            return SimpleNamespace(wait=lambda: SimpleNamespace(status=status))

        def start_update(pipeline_id: str) -> SimpleNamespace:
            self.refreshed.append(pipeline_id)
            return SimpleNamespace(update_id="update-1")

        def get_update(pipeline_id: str, update_id: str) -> SimpleNamespace:
            return SimpleNamespace(update=SimpleNamespace(state=refresh_state))

        self.postgres = SimpleNamespace(get_synced_table=get_synced_table, create_synced_table=create_synced_table)
        self.pipelines = SimpleNamespace(start_update=start_update, get_update=get_update)


def test_synced_table_name_keeps_environments_apart() -> None:
    """Every environment shares one Lakebase database, so the environment is part of the name."""
    assert synced_table_name(CONFIG, "dev") == "llmops_dev.nicholasmisawo.fcr_chunks_dev"


def test_synced_table_spec_keys_by_id_and_stores_embeddings_as_vectors() -> None:
    """Chunks are keyed by id, and the embedding arrives in Postgres as a pgvector vector(1024)."""
    spec = synced_table_spec(CONFIG, SOURCE)

    assert spec.source_table_full_name == SOURCE
    assert spec.primary_key_columns == ["id"]
    [override] = spec.type_overrides
    assert (override.column_name, override.size) == ("embedding", 1024)
    assert override.pg_type == SyncedTableSyncedTableSpecPgSpecificType.PG_SPECIFIC_TYPE_VECTOR


def test_synced_table_spec_refreshes_on_request_on_the_project_branch_under_the_usage_policy() -> None:
    """The sync is TRIGGERED, targets the provisioned project's production branch, and is billed to the usage policy."""
    spec = synced_table_spec(CONFIG, SOURCE)

    assert spec.scheduling_policy == SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.TRIGGERED
    assert spec.branch == "projects/lb-llmops-nicholasmisawo/branches/production"
    assert spec.postgres_database == "databricks_postgres"
    assert spec.new_pipeline_spec.budget_policy_id == CONFIG.usage_policy_id


def test_sync_to_lakebase_creates_a_missing_synced_table() -> None:
    """The first run creates the synced table, which copies every row."""
    workspace = FakeWorkspace()

    assert sync_to_lakebase(workspace, "c.s.fcr_chunks_dev", synced_table_spec(CONFIG, SOURCE)) == "created"
    assert workspace.created == ["c.s.fcr_chunks_dev"]
    assert workspace.refreshed == []


def test_sync_to_lakebase_refreshes_an_existing_synced_table() -> None:
    """Later runs refresh the existing sync pipeline and wait for it, instead of creating another."""
    workspace = FakeWorkspace(existing_pipeline_id="pipeline-1")

    assert sync_to_lakebase(workspace, "c.s.fcr_chunks_dev", synced_table_spec(CONFIG, SOURCE)) == "refreshed"
    assert workspace.refreshed == ["pipeline-1"]
    assert workspace.created == []


def test_sync_to_lakebase_fails_when_creating_the_synced_table_fails() -> None:
    """A synced table that ends up OFFLINE_FAILED fails the run instead of reporting success."""
    workspace = FakeWorkspace(created_state=SyncedTableState.SYNCED_TABLE_OFFLINE_FAILED)

    with pytest.raises(RuntimeError, match="extension missing"):
        sync_to_lakebase(workspace, "c.s.fcr_chunks_dev", synced_table_spec(CONFIG, SOURCE))


def test_sync_to_lakebase_fails_when_a_refresh_fails() -> None:
    """A refresh whose pipeline update fails fails the run."""
    workspace = FakeWorkspace(existing_pipeline_id="pipeline-1", refresh_state=UpdateInfoState.FAILED)

    with pytest.raises(RuntimeError, match="FAILED"):
        sync_to_lakebase(workspace, "c.s.fcr_chunks_dev", synced_table_spec(CONFIG, SOURCE))


def test_endpoint_path_points_at_the_projects_primary_compute() -> None:
    """Connections go to the read-write compute on the production branch."""
    assert endpoint_path(CONFIG) == "projects/lb-llmops-nicholasmisawo/branches/production/endpoints/primary"
