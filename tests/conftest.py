"""Shared pytest fixtures."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def bma_cards_html() -> str:
    """Return a BMA fetchdata fragment: one full card (with duplicate links) and one missing its id."""
    return (FIXTURES / "bma_cards.html").read_text()
