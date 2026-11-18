"""Tests for the Unity Catalog trace setup: experiment bindings, trace grants and release tags."""

from types import SimpleNamespace

import pytest
from databricks.sdk.service.catalog import Privilege, TableType
from mlflow.entities.trace_location import UnityCatalog

from fcr_curator.tracing import (
    TABLE_PRIVILEGES,
    VIEW_PRIVILEGES,
    TraceGrant,
    bind_experiment,
    grant_trace_access,
    release_tags,
    trace_grants,
)

LOCATION = UnityCatalog(catalog_name="llmops_dev", schema_name="nicholasmisawo", table_prefix="llmops")
EXPERIMENT = "/llmops/nicholasmisawo-dev"


class FakeMlflow:
    """Stands in for mlflow's experiment lookups, recording how set_experiment was called."""

    def __init__(self, existing: SimpleNamespace | None) -> None:
        self.existing = existing
        self.set_calls: list[dict] = []

    def get_experiment_by_name(self, name: str) -> SimpleNamespace | None:
        """Return the existing experiment, whatever the name."""
        return self.existing

    def set_experiment(self, **kwargs: object) -> dict:
        """Record the call and return its arguments."""
        self.set_calls.append(kwargs)
        return kwargs


def use_fake_mlflow(monkeypatch: pytest.MonkeyPatch, existing: SimpleNamespace | None) -> FakeMlflow:
    """Patch tracing.py's mlflow with a fake holding existing."""
    fake = FakeMlflow(existing)
    monkeypatch.setattr("fcr_curator.tracing.mlflow", fake)
    return fake


def test_bind_experiment_creates_a_missing_experiment_bound_to_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing experiment is created with its Unity Catalog trace location."""
    fake = use_fake_mlflow(monkeypatch, existing=None)

    bind_experiment(EXPERIMENT, LOCATION)

    assert fake.set_calls == [{"experiment_name": EXPERIMENT, "trace_location": LOCATION}]


def test_bind_experiment_reuses_an_experiment_already_bound_to_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """An experiment bound to the same catalog, schema and prefix is selected, not recreated."""
    same = UnityCatalog(catalog_name="llmops_dev", schema_name="nicholasmisawo", table_prefix="llmops")
    fake = use_fake_mlflow(monkeypatch, SimpleNamespace(experiment_id="42", trace_location=same))

    bind_experiment(EXPERIMENT, LOCATION)

    assert fake.set_calls == [{"experiment_id": "42"}]


@pytest.mark.parametrize(
    "bound_to",
    [
        UnityCatalog(catalog_name="llmops_dev", schema_name="nicholasmisawo", table_prefix="llmops_demo"),
        None,  # A workspace experiment, with no Unity Catalog binding
    ],
)
def test_bind_experiment_refuses_an_experiment_bound_elsewhere(
    monkeypatch: pytest.MonkeyPatch, bound_to: UnityCatalog | None
) -> None:
    """An experiment with another binding, or none, is never silently converted."""
    fake = use_fake_mlflow(monkeypatch, SimpleNamespace(experiment_id="42", trace_location=bound_to))

    with pytest.raises(ValueError, match="new experiment path"):
        bind_experiment(EXPERIMENT, LOCATION)
    assert fake.set_calls == []


def table(name: str, table_type: TableType = TableType.MANAGED) -> SimpleNamespace:
    """Build a table in llmops_dev.nicholasmisawo as the SDK lists it."""
    return SimpleNamespace(name=name, full_name=f"llmops_dev.nicholasmisawo.{name}", table_type=table_type)


def test_trace_grants_cover_only_the_prefixed_objects_tables_to_write_and_views_to_read() -> None:
    """Trace tables get SELECT and MODIFY, trace views SELECT; other tables and other prefixes nothing."""
    tables = [
        table("llmops_otel_spans"),
        table("llmops_trace_unified", TableType.VIEW),
        table("llmops_demo_otel_spans"),
        table("fcr_register"),
    ]

    grants = trace_grants(tables, "llmops")

    assert grants == [
        TraceGrant("llmops_dev.nicholasmisawo.llmops_otel_spans", TABLE_PRIVILEGES),
        TraceGrant("llmops_dev.nicholasmisawo.llmops_trace_unified", VIEW_PRIVILEGES),
        TraceGrant("llmops_dev.nicholasmisawo.llmops_demo_otel_spans", TABLE_PRIVILEGES),
    ]


def test_grant_trace_access_adds_each_grant_for_the_principal() -> None:
    """Each object gets one change adding its privileges for the principal."""
    updates: list[dict] = []
    workspace = SimpleNamespace(grants=SimpleNamespace(update=lambda **kwargs: updates.append(kwargs)))

    grant_trace_access(workspace, [TraceGrant("c.s.llmops_otel_spans", TABLE_PRIVILEGES)], "app-id")

    [update] = updates
    [change] = update["changes"]
    assert (update["full_name"], change.principal, change.add) == (
        "c.s.llmops_otel_spans",
        "app-id",
        [Privilege.SELECT, Privilege.MODIFY],
    )


def test_release_tags_fall_back_to_local_outside_a_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without deployment variables, every tag reads local."""
    for name in ("GIT_SHA", "DATABRICKS_APP_NAME", "MODEL_SERVING_ENDPOINT_NAME", "MODEL_VERSION"):
        monkeypatch.delenv(name, raising=False)

    assert release_tags() == {"git_sha": "local", "deployment_name": "local", "model_version": "local"}


def test_release_tags_name_the_app_before_the_serving_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment's commit, version and App name are what the trace carries."""
    monkeypatch.setenv("GIT_SHA", "f16aa9a")
    monkeypatch.setenv("MODEL_VERSION", "3")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "fcr-agent-dev")
    monkeypatch.setenv("MODEL_SERVING_ENDPOINT_NAME", "fcr-endpoint")

    assert release_tags() == {"git_sha": "f16aa9a", "deployment_name": "fcr-agent-dev", "model_version": "3"}
