"""Tests for the agent's tools: their input checks, what they return, and how errors are raised."""

import json

import pytest

from fcr_curator.agent_tools import (
    EXCERPT_CHARS,
    MAX_PAGE_CHARS,
    MAX_SEARCH_RESULTS,
    ToolInputError,
    extract_fcr_content_tool,
    get_fcr_details_tool,
    search_fcr_tool,
)
from fcr_curator.search import NO_FILTERS, SearchFilters, SearchHit

FILE_URL = "https://www.wiltonre.bm/fcr-2024.pdf"


def hit(text: str = "BSCR ratio of 245%", page: int = 16, score: float | None = 0.0323) -> SearchHit:
    """Build a Wilton Re 2024 hit on the given page."""
    return SearchHit("key", text, "Wilton Re", 2024, page, f"{FILE_URL}#page={page}", "Long Term Class E", score)


class FakeStore:
    """Records searches and page lookups, returning the hits it was given."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.searches: list[tuple[str, SearchFilters, int]] = []
        self.pages: list[str] = []

    def hybrid_search(self, query: str, filters: SearchFilters = NO_FILTERS, limit: int = 20) -> list[SearchHit]:
        """Record the search and return every hit."""
        self.searches.append((query, filters, limit))
        return self.hits

    def page(self, citation_url: str) -> list[SearchHit]:
        """Record the page asked for and return every hit."""
        self.pages.append(citation_url)
        return self.hits


def test_search_fcr_passes_the_filters_and_returns_citable_passages() -> None:
    """The model's filters reach the search, and each passage says how to fetch its page."""
    store = FakeStore([hit()])

    [passage] = json.loads(
        search_fcr_tool(store).exec_fn(query=" solvency ", insurer="Wilton Re", reporting_year=2024, num_results=3)
    )

    assert store.searches == [("solvency", SearchFilters(institution="Wilton Re", reporting_year=2024), 3)]
    assert (passage["file_url"], passage["page"], passage["score"]) == (FILE_URL, 16, 0.0323)


def test_search_fcr_cuts_excerpts_short() -> None:
    """A long chunk comes back as an excerpt; extract_fcr_content gives the whole page."""
    [passage] = json.loads(search_fcr_tool(FakeStore([hit("x" * 5000)])).exec_fn(query="solvency"))

    assert len(passage["excerpt"]) == EXCERPT_CHARS


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "  "},
        {"query": "solvency", "fcr_class": "Class 9"},
        {"query": "solvency", "reporting_year": 24},
        {"query": "solvency", "num_results": MAX_SEARCH_RESULTS + 1},
        {"query": "solvency", "insurer": "Re"},
    ],
)
def test_search_fcr_rejects_arguments_it_cant_use(arguments: dict) -> None:
    """Blank queries, unknown classes, implausible years, too many results and vague names are refused."""
    store = FakeStore([hit()])

    with pytest.raises(ToolInputError):
        search_fcr_tool(store).exec_fn(**arguments)

    assert store.searches == []


def test_extract_fcr_content_joins_the_pages_chunks_in_order() -> None:
    """The page's chunks are read by its citation URL and joined in the order the store returns them."""
    store = FakeStore([hit("First"), hit("Second")])

    page = json.loads(extract_fcr_content_tool(store).exec_fn(file_url=FILE_URL, page=16))

    assert store.pages == [f"{FILE_URL}#page=16"]
    assert (page["text"], page["truncated"], page["insurer"]) == ("First\n\nSecond", False, "Wilton Re")


def test_extract_fcr_content_truncates_a_very_long_page_and_says_so() -> None:
    """The text is capped so one page can't crowd out the rest of the turn."""
    page = json.loads(extract_fcr_content_tool(FakeStore([hit("x" * (MAX_PAGE_CHARS + 1))])).exec_fn(FILE_URL, 1))

    assert (len(page["text"]), page["truncated"]) == (MAX_PAGE_CHARS, True)


def test_extract_fcr_content_raises_lookup_error_for_a_page_not_in_the_knowledge_base() -> None:
    """An unknown page is a LookupError, which the agent hands back to the model."""
    with pytest.raises(LookupError, match="No text for page 99"):
        extract_fcr_content_tool(FakeStore([])).exec_fn(file_url=FILE_URL, page=99)


@pytest.mark.parametrize(
    ("file_url", "page"), [(f"{FILE_URL}#page=16", 16), ("wiltonre.bm/fcr.pdf", 16), (FILE_URL, 0)]
)
def test_extract_fcr_content_rejects_a_url_with_a_page_part_a_bare_domain_or_page_zero(
    file_url: str, page: int
) -> None:
    """file_url must be a plain http(s) PDF link, and pages count from 1."""
    with pytest.raises(ToolInputError):
        extract_fcr_content_tool(FakeStore([hit()])).exec_fn(file_url=file_url, page=page)


def test_get_fcr_details_calls_the_uc_function_with_the_insurer() -> None:
    """The name is checked, then passed to the UC function's MCP tool, whose reply is returned as is."""
    calls: list[dict] = []

    def call_uc_function(**kwargs: str) -> str:
        calls.append(kwargs)
        return '[{"reporting_year": 2024}]'

    reply = get_fcr_details_tool(call_uc_function).exec_fn(insurer=" Wilton Re ")

    assert (calls, reply) == ([{"insurer": "Wilton Re"}], '[{"reporting_year": 2024}]')


def test_get_fcr_details_refuses_a_name_too_short_to_match_one_insurer() -> None:
    """'Re' would match almost every reinsurer, so the UC function isn't called."""
    with pytest.raises(ToolInputError, match="too short"):
        get_fcr_details_tool(lambda **kwargs: "[]").exec_fn(insurer="Re")
