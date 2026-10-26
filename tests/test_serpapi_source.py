"""Tests for discovering FCRs through SerpAPI Google search."""

import pytest

from fcr_curator.search_plan import PlannedQuery
from fcr_curator.sources.serpapi import (
    NO_RESULTS_ERROR,
    document_id,
    fetch_fcr_documents,
    merge_rows,
    parse_results,
    search_page,
    searches_left,
)


class FakeResponse:
    """The parts of requests.Response that the source reads."""

    def __init__(self, payload: dict | None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self) -> dict:
        """Return the payload, or fail like requests does on a non-JSON body."""
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class FakeSession:
    """Serves queued responses in order and records the params of each request."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def get(self, _url: str, params: dict, timeout: int) -> FakeResponse:  # noqa: ARG002
        """Record the request and return the next queued response."""
        self.requests.append(params)
        return self.responses.pop(0)


def full_page(title: str) -> FakeResponse:
    """Return a page of 10 results, all with the given title."""
    results = [{"position": i, "title": title, "link": f"https://x.com/{title[:3]}{i}.pdf"} for i in range(1, 11)]
    return FakeResponse({"organic_results": results})


def planned(query: str = "q", max_pages: int = 2) -> PlannedQuery:
    """Return a planned query that may use up to max_pages pages."""
    return PlannedQuery(query, "discovery", max_pages)


def test_document_id_is_the_same_for_urls_differing_in_host_case_or_fragment() -> None:
    """One file found under two spellings of its URL gets one id."""
    assert document_id("https://WWW.x.com/a.pdf#page=2") == document_id("https://www.x.com/a.pdf")
    assert document_id("https://x.com/a.pdf") != document_id("https://x.com/b.pdf")


def test_parse_results_maps_results_and_skips_ones_without_a_link(serpapi_page: dict) -> None:
    """Each linked result becomes a source row carrying the query that found it."""
    rows = parse_results(serpapi_page["organic_results"], "q")

    assert [row["search_rank"] for row in rows] == [1, 2]
    assert rows[0]["pdf_url"] == "https://www.example-re.com/files/FCR-2024.pdf"
    assert rows[0]["domain"] == "example-re.com"
    assert rows[0]["site_name"] == "Example Re"
    assert rows[0]["search_queries"] == ["q"]


def test_merge_rows_keeps_the_best_rank_and_every_query() -> None:
    """A URL found by two queries is one row with its best rank and both queries, sorted."""
    row = {"document_id": "a", "title": "t", "search_rank": 5, "search_queries": ["z query"]}
    better = {**row, "search_rank": 2, "search_queries": ["a query"]}

    [merged] = merge_rows([row, better])

    assert merged["search_rank"] == 2
    assert merged["search_queries"] == ["a query", "z query"]


def test_search_page_treats_google_finding_nothing_as_an_empty_page() -> None:
    """SerpAPI's no-results error is an empty page, not a failure."""
    session = FakeSession(FakeResponse({"error": NO_RESULTS_ERROR}))

    assert search_page(session, "key", "q") == []


def test_search_page_keeps_results_english_bermuda_ranked_and_uncorrected() -> None:
    """Every search asks for English documents, ranked as if from Bermuda, for the exact query given."""
    session = FakeSession(FakeResponse({"organic_results": []}))

    search_page(session, "key", "q", start=10)

    [params] = session.requests
    assert (params["q"], params["start"]) == ("q", 10)
    assert (params["gl"], params["lr"], params["nfpr"]) == ("bm", "lang_en", 1)
    assert "cr" not in params and "tbs" not in params


@pytest.mark.parametrize(
    "response",
    [FakeResponse({"error": "Invalid API key."}, 401), FakeResponse(None, 503)],
)
def test_search_page_raises_on_any_other_error_without_leaking_the_key(response: FakeResponse) -> None:
    """A bad key, an exhausted plan or an outage fails the search, naming the query but not the key."""
    with pytest.raises(RuntimeError, match="'q'") as error:
        search_page(FakeSession(response), "secret-key", "q")

    assert "secret-key" not in str(error.value)


def test_searches_left_counts_extra_credits() -> None:
    """The account's total left, including extra credits, is what a run may spend."""
    session = FakeSession(FakeResponse({"plan_searches_left": 40, "extra_credits": 10, "total_searches_left": 50}))

    assert searches_left(session, "key") == 50


def test_searches_left_raises_when_the_account_check_fails() -> None:
    """Without a trustworthy count, the run must not start spending searches."""
    with pytest.raises(RuntimeError, match="account check"):
        searches_left(FakeSession(FakeResponse({"error": "Invalid API key."}, 401)), "key")


def test_fetch_stops_paging_once_a_page_is_short(serpapi_page: dict) -> None:
    """A page with fewer than 10 results is the last, so no further page is requested or billed."""
    session = FakeSession(FakeResponse(serpapi_page))

    run = fetch_fcr_documents(session, "key", [planned(max_pages=3)], max_searches=10, pause_seconds=0)

    assert len(session.requests) == 1
    assert {document["source"] for document in run.documents} == {"serpapi"}


def test_fetch_asks_the_next_page_after_a_full_page_of_fcrs(serpapi_page: dict) -> None:
    """A full page holding FCR-like results is followed by a request starting at result 10."""
    session = FakeSession(full_page("Example Re FCR 2024"), FakeResponse(serpapi_page))

    fetch_fcr_documents(session, "key", [planned()], max_searches=10, pause_seconds=0)

    assert [request["start"] for request in session.requests] == [0, 10]


def test_fetch_stops_paging_when_a_full_page_has_nothing_fcr_like() -> None:
    """A query that has drifted off FCRs isn't paged further, saving the search."""
    session = FakeSession(full_page("Annual Report 2024"))

    fetch_fcr_documents(session, "key", [planned()], max_searches=10, pause_seconds=0)

    assert len(session.requests) == 1


def test_fetch_never_exceeds_max_searches(serpapi_page: dict) -> None:
    """The run's cap holds across queries, so later queries in the plan are left for another run."""
    session = FakeSession(*(FakeResponse(serpapi_page) for _ in range(3)))

    run = fetch_fcr_documents(
        session, "key", [planned("a"), planned("b"), planned("c")], max_searches=2, pause_seconds=0
    )

    assert [entry["query"] for entry in run.log] == ["a", "b"]


def test_fetch_logs_each_search_with_its_fcr_like_results(serpapi_page: dict) -> None:
    """Each billed search is logged with its page, result count and FCR-like count."""
    run = fetch_fcr_documents(
        FakeSession(FakeResponse(serpapi_page)), "key", [planned()], max_searches=5, pause_seconds=0
    )

    [entry] = run.log
    assert (entry["query"], entry["reason"], entry["page_start"]) == ("q", "discovery", 0)
    assert (entry["results"], entry["fcr_results"]) == (2, 2)


def test_fetch_keeps_earlier_results_when_a_later_search_fails(serpapi_page: dict) -> None:
    """An error stops the run, but searches already paid for keep their results and log entries."""
    session = FakeSession(
        FakeResponse(serpapi_page), FakeResponse({"error": "Your account has run out of searches."}, 429)
    )

    run = fetch_fcr_documents(session, "key", [planned("a"), planned("b")], max_searches=5, pause_seconds=0)

    assert len(run.log) == 1
    assert run.documents
    assert "run out of searches" in run.error
