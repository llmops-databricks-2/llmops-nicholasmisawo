"""MLflow tracing in Unity Catalog: experiment bindings, the trace writer's grants, and release tags.

The course's lecture 4 setup (notebook 4.0) as functions the notebooks and the agent share:

- bind_experiment creates an experiment bound to Unity Catalog trace tables, or checks an existing one's
  binding, and never silently converts one bound elsewhere.
- trace_grants works out what the identity writing traces needs on them. Databricks requires explicit
  SELECT and MODIFY on the trace tables (and SELECT on the views); ALL PRIVILEGES on the schema isn't enough.
- release_tags reads the code and deployment identifiers each trace is tagged with.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import PermissionsChange, Privilege, TableInfo, TableType
from mlflow.entities import Experiment
from mlflow.entities.trace_location import UnityCatalog

from fcr_curator.config import ProjectConfig

# Searching traces kept in Unity Catalog runs SQL on this warehouse; read in the process that searches
SQL_WAREHOUSE_ENV = "MLFLOW_TRACING_SQL_WAREHOUSE_ID"
TABLE_PRIVILEGES = (Privilege.SELECT, Privilege.MODIFY)
VIEW_PRIVILEGES = (Privilege.SELECT,)
UNSET = "local"  # Release tags outside a deployment, so local runs are still distinguishable


@dataclass(frozen=True)
class TraceGrant:
    """Privileges one principal needs on one trace table or view."""

    full_name: str
    privileges: tuple[Privilege, ...]


def trace_location(config: ProjectConfig, table_prefix: str) -> UnityCatalog:
    """Return where traces with table_prefix live: <catalog>.<schema>.<table_prefix>_otel_spans and beside it."""
    return UnityCatalog(catalog_name=config.catalog, schema_name=config.schema, table_prefix=table_prefix)


def same_location(first: UnityCatalog, second: UnityCatalog) -> bool:
    """Return whether two trace locations name the same catalog, schema and table prefix."""
    return (first.catalog_name, first.schema_name, first.table_prefix) == (
        second.catalog_name,
        second.schema_name,
        second.table_prefix,
    )


def bind_experiment(experiment_name: str, location: UnityCatalog) -> Experiment:
    """Return the experiment, bound to location: created if it's missing, checked if it exists.

    Raises ValueError if the experiment exists with another trace location, as the course's 4.0 does:
    the same name can refer to an experiment created with a different storage binding.
    """
    existing = mlflow.get_experiment_by_name(experiment_name)
    if existing is None:
        return mlflow.set_experiment(experiment_name=experiment_name, trace_location=location)
    bound_to = existing.trace_location
    if not isinstance(bound_to, UnityCatalog) or not same_location(bound_to, location):
        raise ValueError(
            f"Experiment {experiment_name} is bound to {bound_to}, not {location}; "
            "configure a new experiment path in project_config.yml rather than reusing this one"
        )
    return mlflow.set_experiment(experiment_id=existing.experiment_id)


def trace_grants(tables: Iterable[TableInfo], table_prefix: str) -> list[TraceGrant]:
    """Return the grants a trace writer needs on the <table_prefix>_ objects: tables to write, views to read."""
    return [
        TraceGrant(table.full_name, VIEW_PRIVILEGES if table.table_type == TableType.VIEW else TABLE_PRIVILEGES)
        for table in tables
        if table.name.startswith(f"{table_prefix}_")
    ]


def grant_trace_access(workspace: WorkspaceClient, grants: Iterable[TraceGrant], principal: str) -> None:
    """Add each grant for principal; granting what it already holds changes nothing."""
    for grant in grants:
        workspace.grants.update(
            securable_type="table",
            full_name=grant.full_name,
            changes=[PermissionsChange(add=list(grant.privileges), principal=principal)],
        )


def use_experiment(config: ProjectConfig, experiment_name: str) -> Experiment:
    """Send this process's traces to experiment_name, and let it search them through the config's warehouse."""
    os.environ[SQL_WAREHOUSE_ENV] = config.warehouse_id
    return mlflow.set_experiment(experiment_name)


def release_tags() -> dict[str, str]:
    """Return the code version and deployment a trace came from, as the course's agent tags them.

    Deployments set GIT_SHA, MODEL_VERSION and the App or endpoint name; anything unset reads "local".
    """
    deployment = os.getenv("DATABRICKS_APP_NAME") or os.getenv("MODEL_SERVING_ENDPOINT_NAME") or UNSET
    return {
        "git_sha": os.getenv("GIT_SHA") or UNSET,
        "deployment_name": deployment,
        "model_version": os.getenv("MODEL_VERSION") or UNSET,
    }
