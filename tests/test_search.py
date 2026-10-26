"""Tests for searching the synced FCR chunks: filters, keyword queries and rank fusion."""

from fcr_curator.search import SearchFilters, SearchHit, any_word_query, reciprocal_rank_fusion


def hit(hit_id: str) -> SearchHit:
    """Build a search hit with only an id that matters."""
    return SearchHit(hit_id, "text", "Example Re", 2024, 0, "https://x.com/a.pdf")


def test_filters_without_values_match_everything() -> None:
    """No institution or year means no parameters to filter on."""
    _, params = SearchFilters().where()

    assert params == []


def test_filters_match_institution_loosely_and_year_exactly() -> None:
    """The institution becomes a contains-pattern on its key; the year is compared as given."""
    _, params = SearchFilters(institution="Wilton Re Ltd.", reporting_year=2024).where()

    assert params == ["%wiltonre%", 2024]


def test_any_word_query_matches_any_word_and_drops_punctuation() -> None:
    """Every word is an alternative, so a chunk needs only one of them to match."""
    assert any_word_query("BSCR ratio, ECR?") == "BSCR or ratio or ECR"


def test_rank_fusion_puts_hits_found_by_both_searches_first() -> None:
    """A hit both rankings return outranks hits only one returns, even ranked first there."""
    fused = reciprocal_rank_fusion([[hit("a"), hit("both")], [hit("b"), hit("both")]])

    assert fused[0].id == "both"
    assert {fused_hit.id for fused_hit in fused} == {"a", "b", "both"}


def test_rank_fusion_returns_each_hit_once_up_to_the_limit() -> None:
    """Duplicates across rankings are merged, and the result is cut to the limit."""
    fused = reciprocal_rank_fusion([[hit("a"), hit("b"), hit("c")], [hit("a")]], limit=2)

    assert [fused_hit.id for fused_hit in fused] == ["a", "b"]
