"""Shared pytest fixtures."""

import json
from pathlib import Path

import mlflow
import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"
SOURCES_FILE = Path(__file__).resolve().parents[1] / "dbt/models/staging/_sources.yml"


@pytest.fixture
def serpapi_page() -> dict:
    """Return a SerpAPI Google response: a Financial Condition Report, an SFCR, and a result with no link."""
    return json.loads((FIXTURES / "serpapi_google.json").read_text())


@pytest.fixture(scope="session")
def landed_columns() -> dict[str, set[str]]:
    """Return each landed table's columns as dbt defines them, minus landed_at, which landing.py stamps."""
    [landed] = yaml.safe_load(SOURCES_FILE.read_text())["sources"]
    return {
        table["name"]: {column["name"] for column in table["columns"]} - {"landed_at"} for table in landed["tables"]
    }


@pytest.fixture(scope="session", autouse=True)
def no_tracing(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Switch tracing off, with any MLflow store in a temporary folder; a test reading traces enables it.

    Enabling tracing creates MLflow's default store, ./mlflow.db, so the session never re-enables it there.
    """
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path_factory.mktemp('mlflow') / 'mlflow.db'}")
    mlflow.tracing.disable()
