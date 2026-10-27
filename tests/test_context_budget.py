"""Tests for packing search hits into a cited context within a token budget."""

from fcr_curator.context_budget import build_context, cite, estimate_tokens
from fcr_curator.search import SearchHit


def hit(hit_id: str, chars: int) -> SearchHit:
    """Build a hit whose text is chars long."""
    return SearchHit(hit_id, "x" * chars, "Example Re", 2024, 5, "https://x.com/a.pdf#page=5")


def test_cite_names_company_year_page_and_a_link_to_that_page() -> None:
    """A source shows its number, company, FCR year and page, and links to that page of the PDF."""
    cited = cite(2, hit("a", 3))

    assert cited.startswith("[2] Example Re, FCR 2024, page 5\n")
    assert cited.endswith("Source: https://x.com/a.pdf#page=5")


def test_context_stays_within_the_token_budget() -> None:
    """Hits are added best first until the next one would go over the budget."""
    hits = [hit("a", 600), hit("b", 600), hit("c", 600)]

    context = build_context(hits, max_tokens=500)

    assert [source.id for source in context.sources] == ["a", "b"]
    assert context.tokens <= 500


def test_context_skips_a_hit_too_big_for_the_space_left_but_keeps_smaller_ones() -> None:
    """An oversized hit doesn't end packing: a later hit that fits still goes in."""
    context = build_context([hit("small", 300), hit("huge", 9000), hit("fits", 300)], max_tokens=300)

    assert [source.id for source in context.sources] == ["small", "fits"]


def test_context_numbers_sources_in_the_order_they_appear() -> None:
    """Citation numbers follow what's in the context, with no gaps for skipped hits."""
    context = build_context([hit("a", 30), hit("huge", 9000), hit("b", 30)], max_tokens=200)

    assert "[1] " in context.text and "[2] " in context.text and "[3] " not in context.text


def test_context_of_no_hits_is_empty() -> None:
    """Nothing to pack gives an empty context."""
    context = build_context([])

    assert (context.text, context.sources, context.tokens) == ("", (), 0)


def test_estimate_tokens_rounds_up() -> None:
    """Any text, however short, counts as at least one token."""
    assert estimate_tokens("x") == 1
