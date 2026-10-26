"""Shared pytest fixtures."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def serpapi_page() -> dict:
    """Return a SerpAPI Google response: an FCR, an SFCR, and a result with no link."""
    return json.loads((FIXTURES / "serpapi_google.json").read_text())
