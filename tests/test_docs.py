"""Tests that the docs' copies of the agent diagram stay the same."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_the_agent_page_embeds_the_diagram_source_unchanged() -> None:
    """08-agent.md shows the diagram on GitHub; the .mmd is its source to edit, so the two must match."""
    page = (DOCS / "08-agent.md").read_text()
    [embedded] = re.findall(r"```mermaid\n(.*?)```", page, re.DOTALL)

    assert embedded == (DOCS / "diagrams" / "agent-sequence.mmd").read_text()
