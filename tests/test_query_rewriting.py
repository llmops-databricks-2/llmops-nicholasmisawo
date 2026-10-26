"""Tests for rewriting a question into search text and filters."""

from datetime import date

import pytest

from fcr_curator.query_rewriting import rewrite_query
from fcr_curator.search import SearchFilters

TODAY = date(2026, 10, 26)


def replying(reply: str) -> object:
    """Return a fake chat model that always gives this reply."""
    return lambda instructions, message: reply


def test_rewrite_returns_search_text_and_filters() -> None:
    """The model's search text, insurer and year become the rewritten query."""
    chat = replying('{"search_text": "BSCR ratio", "institution": "Wilton Re", "reporting_year": 2024}')

    rewritten = rewrite_query("How solvent was Wilton Re in 2024?", chat, TODAY)

    assert rewritten.search_text == "BSCR ratio"
    assert rewritten.filters == SearchFilters(institution="Wilton Re", reporting_year=2024)


def test_rewrite_leaves_filters_unset_when_none_are_named() -> None:
    """A question with no insurer or year searches every FCR."""
    chat = replying('{"search_text": "catastrophe risk", "institution": null, "reporting_year": null}')

    assert rewrite_query("Who reports catastrophe risk?", chat, TODAY).filters == SearchFilters()


def test_rewrite_tells_the_model_what_last_year_means() -> None:
    """The instructions give today's date, so 'last year' resolves to the previous calendar year."""
    seen: list[str] = []

    def chat(instructions: str, message: str) -> str:
        seen.append(instructions)
        return '{"search_text": "x"}'

    rewrite_query("Latest BSCR ratio?", chat, TODAY)

    assert "2026-10-26" in seen[0] and "2025" in seen[0]


@pytest.mark.parametrize("reply", ['{"search_text": ""}', '{"institution": "Wilton Re"}', "not json"])
def test_rewrite_fails_without_usable_search_text(reply: str) -> None:
    """An empty, missing or unparseable reply is an error, not a guessed search."""
    with pytest.raises(ValueError):
        rewrite_query("BSCR ratio?", replying(reply), TODAY)
