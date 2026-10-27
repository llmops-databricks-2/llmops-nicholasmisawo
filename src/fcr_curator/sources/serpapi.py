"""Find FCR PDFs with Google search through SerpAPI (https://serpapi.com/search-api).

Bermuda insurers publish FCRs on their own websites, so there is no single list to scrape; we search
instead. fetch_fcr_documents asks the planned queries (see search_plan.py) and returns:

- documents: one row per distinct result URL, for raw_fcr_search_hits
- log: one row per search made, for the search log table, so later runs know what was asked

Nothing is downloaded here, and whether a result really is an FCR is decided later, in curation.py.
"""

import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import NamedTuple

import requests
from loguru import logger

from fcr_curator.curation import classify_doc_kind
from fcr_curator.search_plan import PlannedQuery
from fcr_curator.urls import domain, normalize_url, url_document_id

SERPAPI_SEARCH = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT = "https://serpapi.com/account.json"  # Free to call: doesn't use a search
SOURCE = "serpapi"
RESULTS_PER_PAGE = 10
REQUEST_TIMEOUT_SECONDS = 60
PAUSE_BETWEEN_SEARCHES_SECONDS = 0.5
# SerpAPI reports "no results" as an error, but for us it's just an empty page
NO_RESULTS_ERROR = "Google hasn't returned any results for this query."

# Sent with every search to keep results on target (https://serpapi.com/search-api)
GOOGLE_PARAMS = {
    "engine": "google",
    "hl": "en",  # Interface language
    "gl": "bm",  # Search as if from Bermuda: ranks Bermudian pages higher, excludes nothing
    "lr": "lang_en",  # English documents only; drops e.g. German and French EU SFCRs
    "nfpr": 1,  # Search our exact query, not Google's auto-corrected version of it
}
# Deliberately not used: cr=countryBM hides FCRs hosted outside Bermuda (most are on .com sites), and
# tbs date filters rely on Google's dates for PDFs, which are often the crawl date, not the publication date


class SearchRun(NamedTuple):
    """The outcome of one run of searches."""

    documents: list[dict]  # One row per distinct result URL
    log: list[dict]  # One row per search made (each one used a search from the plan)
    error: str | None  # Why the run stopped early, if it did


# --- URLs -------------------------------------------------------------------------------------


def document_id(url: str) -> str:
    """Return a stable id for a result: 'serpapi_' plus a short hash of its URL."""
    return url_document_id(url, SOURCE)


# --- SerpAPI calls ----------------------------------------------------------------------------


def _get_json(session: requests.Session, url: str, params: dict) -> tuple[dict, requests.Response]:
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        return response.json(), response
    except ValueError:  # An HTML error page, e.g. during an outage; callers check the status code
        return {}, response


def searches_left(session: requests.Session, api_key: str) -> int:
    """Return how many searches the account has left this month, extra credits included."""
    payload, response = _get_json(session, SERPAPI_ACCOUNT, {"api_key": api_key})
    if payload.get("error") or not response.ok:
        raise RuntimeError(f"SerpAPI account check failed (HTTP {response.status_code}): {payload.get('error')}")
    return int(payload.get("total_searches_left", payload.get("plan_searches_left", 0)))


def search_page(session: requests.Session, api_key: str, query: str, start: int = 0) -> list[dict]:
    """Return one page of Google results for a query; [] if Google found nothing.

    Any other SerpAPI error (bad key, plan used up) raises RuntimeError. The message names the query,
    never the request URL, because the URL contains the API key.
    """
    params = {**GOOGLE_PARAMS, "q": query, "start": start, "api_key": api_key}
    payload, response = _get_json(session, SERPAPI_SEARCH, params)
    error = payload.get("error")
    if error == NO_RESULTS_ERROR:
        return []
    if error or not response.ok:
        raise RuntimeError(f"SerpAPI search failed for {query!r} (HTTP {response.status_code}): {error}")
    return payload.get("organic_results", [])


# --- Turning results into rows ----------------------------------------------------------------


def parse_results(results: list[dict], query: str, rank_offset: int = 0) -> list[dict]:
    """Turn Google results into search-results rows, skipping any result without a link."""
    return [
        {
            "document_id": document_id(result["link"]),
            "title": result.get("title") or "",
            "snippet": result.get("snippet"),
            "pdf_url": normalize_url(result["link"]),
            "displayed_link": result.get("displayed_link"),
            "site_name": result.get("source"),
            "domain": domain(result["link"]),
            "search_queries": [query],
            "search_rank": result.get("position") or rank_offset + index,  # 1-based across pages
        }
        for index, result in enumerate(results, start=1)
        if result.get("link")
    ]


def count_fcr_like(rows: Iterable[dict]) -> int:
    """Count the rows that look like an FCR from their title, filename or snippet."""
    return sum(classify_doc_kind(row["title"], row["pdf_url"], row["snippet"]) == "fcr" for row in rows)


def merge_rows(rows: Iterable[dict]) -> list[dict]:
    """Return one row per URL: the best-ranked copy, listing every query that found it (sorted)."""
    best: dict[str, dict] = {}
    queries: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        url_id = row["document_id"]
        queries[url_id].update(row["search_queries"])
        if url_id not in best or row["search_rank"] < best[url_id]["search_rank"]:
            best[url_id] = row
    # Sorted, so the same queries always compare equal in the search-results MERGE
    return [{**row, "search_queries": sorted(queries[url_id])} for url_id, row in best.items()]


# --- One run ----------------------------------------------------------------------------------


def fetch_fcr_documents(
    session: requests.Session,
    api_key: str,
    plan: Iterable[PlannedQuery],
    max_searches: int,
    pause_seconds: float = PAUSE_BETWEEN_SEARCHES_SECONDS,
) -> SearchRun:
    """Ask the planned queries in order, never making more than max_searches searches.

    A query's next page is only asked if the last page was full and had something FCR-like on it.
    If SerpAPI fails partway, the run stops but keeps what it already found (those searches are paid for).
    """
    run_at = datetime.now(UTC).isoformat()
    rows: list[dict] = []
    log: list[dict] = []

    def finish(error: str | None = None) -> SearchRun:
        documents = [{**row, "source": SOURCE, "ingestion_timestamp": run_at} for row in merge_rows(rows)]
        logger.info(f"{len(log)} searches, {len(rows)} results, {len(documents)} distinct URLs")
        return SearchRun(documents, log, error)

    for planned in plan:
        for page in range(planned.max_pages):
            if len(log) >= max_searches:
                return finish()

            start = page * RESULTS_PER_PAGE
            try:
                results = search_page(session, api_key, planned.query, start=start)
            except RuntimeError as exc:
                return finish(error=str(exc))

            page_rows = parse_results(results, planned.query, rank_offset=start)
            fcr_like = count_fcr_like(page_rows)
            rows.extend(page_rows)
            log.append(
                {
                    "query": planned.query,
                    "reason": planned.reason,
                    "page_start": start,
                    "results": len(page_rows),
                    "fcr_results": fcr_like,
                    "searched_at": run_at,
                }
            )
            time.sleep(pause_seconds)

            # A short page is Google's last; a full page with nothing FCR-like means the query has gone off topic
            if len(results) < RESULTS_PER_PAGE or fcr_like == 0:
                break

    return finish()
