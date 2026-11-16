"""Tests for the agent's Unity Catalog function: its definition, MCP address and registration."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from databricks.sdk.service.sql import StatementState

from fcr_curator.config import ProjectConfig
from fcr_curator.mcp import genie_mcp_url
from fcr_curator.uc_functions import get_fcr_details_sql, register_uc_functions, uc_function_mcp_url

CONFIG = ProjectConfig.from_yaml(str(Path(__file__).resolve().parents[1] / "project_config.yml"), "dev")


def test_get_fcr_details_is_created_in_the_environments_schema_and_reads_its_tables() -> None:
    """The function and the tables it reads are all in the configured catalog and schema."""
    sql = get_fcr_details_sql(CONFIG)

    assert sql.startswith("CREATE OR REPLACE FUNCTION llmops_dev.nicholasmisawo.get_fcr_details(")
    assert "FROM llmops_dev.nicholasmisawo.fcr_register r" in sql
    assert "JOIN llmops_dev.nicholasmisawo.dim_fcr_entities e" in sql


def test_get_fcr_details_ignores_names_too_short_to_match_one_insurer() -> None:
    """A guard on the name's key length stops 'Re' from matching every reinsurer."""
    assert "length(regexp_replace(lower(regexp_replace(insurer," in get_fcr_details_sql(CONFIG)


def test_mcp_urls_address_the_function_and_the_genie_space() -> None:
    """Managed MCP servers are addressed under the workspace host, whatever its trailing slash."""
    assert (
        uc_function_mcp_url("https://host/", CONFIG, "get_fcr_details")
        == "https://host/api/2.0/mcp/functions/llmops_dev/nicholasmisawo/get_fcr_details"
    )
    assert genie_mcp_url("https://host", "space-1") == "https://host/api/2.0/mcp/genie/space-1"


def statement_workspace(state: StatementState, statements: list[dict]) -> SimpleNamespace:
    """Build a workspace whose warehouse ends every statement in state, recording each request."""

    def execute_statement(**request: str) -> SimpleNamespace:
        statements.append(request)
        error = SimpleNamespace(message="PERMISSION_DENIED") if state != StatementState.SUCCEEDED else None
        return SimpleNamespace(status=SimpleNamespace(state=state, error=error))

    return SimpleNamespace(statement_execution=SimpleNamespace(execute_statement=execute_statement))


def test_register_uc_functions_runs_the_definition_on_the_configured_warehouse() -> None:
    """The function is created on the project's warehouse, and its full name returned."""
    statements: list[dict] = []

    created = register_uc_functions(statement_workspace(StatementState.SUCCEEDED, statements), CONFIG)

    assert created == ["llmops_dev.nicholasmisawo.get_fcr_details"]
    assert statements[0]["warehouse_id"] == CONFIG.warehouse_id


def test_register_uc_functions_fails_with_the_warehouses_message() -> None:
    """A statement that doesn't succeed fails registration, quoting why."""
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        register_uc_functions(statement_workspace(StatementState.FAILED, []), CONFIG)
