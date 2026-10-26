"""Discover Bermuda Financial Condition Reports (FCRs) through Google search results from SerpAPI.

Bermuda commercial insurers publish their FCRs on their own websites rather than through the BMA, so
there's no single listing to scrape. Each run asks the queries search_plan chose (via
https://serpapi.com/search-api) and returns one row per distinct result URL, plus one log entry per
search so later runs know what was asked. Nothing is downloaded here, and whether a result really is a
Bermuda FCR is decided later by the curation rules.

Every successful search counts against the SerpAPI plan, including one that finds nothing; cached and
failed searches don't. fetch_fcr_documents never makes more than ``max_searches``.
"""

import hashlib
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import requests
from loguru import logger

from fcr_curator.curation import classify_doc_kind
from fcr_curator.search_plan import PlannedQuery

SERPAPI_SEARCH = "https://serpapi.com/search.json"
# Free: checking the account doesn't count against the plan
SERPAPI_ACCOUNT = "https://serpapi.com/account.json"
SOURCE = "serpapi"
RESULTS_PER_PAGE = 10  # Google pages hold 10 organic results; `start` steps through them
REQUEST_TIMEOUT_SECONDS = 60
PAGE_PAUSE_SECONDS = 0.5
# SerpAPI reports a search that found nothing as an error; it's an empty page, not a failure
NO_RESULTS_ERROR = "Google hasn't returned any results for this query."


class SearchRun(NamedTuple):
    """What one run found, the searches it made, and the error that stopped it early, if any."""

    documents: list[dict]
    log: list[dict]  # One entry per billed search, for the bronze_fcr_search_log table
    error: str | None


def normalize_url(url: str) -> str:
    """Drop the fragment and lower-case the scheme and host, so one file found twice gets one id."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def document_id(url: str) -> str:
    """Return a stable id for a result: the source name and a short hash of its normalized URL."""
    return f"{SOURCE}_{hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]}"


def domain(url: str) -> str:
    """Return the host of a URL without a leading www."""
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")


def _get_json(session: requests.Session, url: str, params: dict) -> tuple[dict, requests.Response]:
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        return response.json(), response
    except ValueError:
        return {}, response  # An HTML error page from a proxy or outage; callers check the status


def searches_left(session: requests.Session, api_key: str) -> int:
    """Return the searches the account has left this month, including extra credits. Costs nothing."""
    payload, response = _get_json(session, SERPAPI_ACCOUNT, {"api_key": api_key})
    if payload.get("error") or not response.ok:
        raise RuntimeError(f"SerpAPI account check failed (HTTP {response.status_code}): {payload.get('error')}")
    return int(payload.get("total_searches_left", payload.get("plan_searches_left", 0)))


def search_page(session: requests.Session, api_key: str, query: str, start: int = 0) -> list[dict]:
    """Return one page of organic Google results for query, or [] when Google found nothing.

    Raises RuntimeError for any other SerpAPI error, such as an invalid key or an exhausted plan.
    """
    params = {"engine": "google", "q": query, "start": start, "hl": "en", "api_key": api_key}
    payload, response = _get_json(session, SERPAPI_SEARCH, params)

    error = payload.get("error")
    if error == NO_RESULTS_ERROR:
        return []
    if error or not response.ok:
        # The key travels in the query string, so report the query rather than the request URL
        raise RuntimeError(f"SerpAPI search failed for {query!r} (HTTP {response.status_code}): {error}")
    return payload.get("organic_results", [])


def parse_results(results: list[dict], query: str, rank_offset: int = 0) -> list[dict]:
    """Map organic results to source rows; rank is the result's position across pages, 1-based."""
    rows = []
    for index, result in enumerate(results, start=1):
        link = result.get("link")
        if not link:
            continue
        rows.append(
            {
                "document_id": document_id(link),
                "title": result.get("title") or "",
                "snippet": result.get("snippet"),
                "pdf_url": normalize_url(link),
                "displayed_link": result.get("displayed_link"),
                "site_name": result.get("source"),
                "domain": domain(link),
                "search_queries": [query],
                "search_rank": result.get("position") or rank_offset + index,
            }
        )
    return rows


def count_fcr_like(rows: Iterable[dict]) -> int:
    """Count the rows whose title, filename or snippet look like an FCR."""
    return sum(classify_doc_kind(row["title"], row["pdf_url"], row["snippet"]) == "fcr" for row in rows)


def merge_rows(rows: Iterable[dict]) -> list[dict]:
    """Combine rows for the same URL: keep the best-ranked one and every query that found it."""
    merged: dict[str, dict] = {}
    for row in rows:
        existing = merged.get(row["document_id"])
        if existing is None:
            merged[row["document_id"]] = {**row, "search_queries": list(row["search_queries"])}
            continue
        queries = existing["search_queries"] + [q for q in row["search_queries"] if q not in existing["search_queries"]]
        best = row if row["search_rank"] < existing["search_rank"] else existing
        merged[row["document_id"]] = {**best, "search_queries": queries}

    for row in merged.values():
        # Sorted so the same set of queries compares equal in the bronze MERGE whatever order they ran in
        row["search_queries"] = sorted(row["search_queries"])
    return list(merged.values())


def _worth_next_page(results: list[dict], fcr_like: int) -> bool:
    # A short page is Google's last; a full page with nothing FCR-like means the query has drifted
    return len(results) == RESULTS_PER_PAGE and fcr_like > 0


def fetch_fcr_documents(
    session: requests.Session,
    api_key: str,
    plan: Iterable[PlannedQuery],
    max_searches: int,
    pause_seconds: float = PAGE_PAUSE_SECONDS,
) -> SearchRun:
    """Ask the planned queries in order until the plan or max_searches runs out.

    A query's next page is asked only while the last one was full and held an FCR-like result. A
    SerpAPI error stops the run but keeps what earlier searches found, so their cost isn't wasted.
    """
    timestamp = datetime.now(UTC).isoformat()
    rows: list[dict] = []
    log: list[dict] = []
    error = None

    for planned in plan:
        for page in range(planned.max_pages):
            if len(log) >= max_searches or error:
                break
            start = page * RESULTS_PER_PAGE
            try:
                results = search_page(session, api_key, planned.query, start=start)
            except RuntimeError as exc:
                error = str(exc)
                break
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
                    "searched_at": timestamp,
                }
            )
            time.sleep(pause_seconds)
            if not _worth_next_page(results, fcr_like):
                break

    documents = merge_rows(rows)
    for row in documents:
        row.update({"source": SOURCE, "ingestion_timestamp": timestamp})

    logger.info(f"{len(log)} SerpAPI searches returned {len(rows)} results, {len(documents)} distinct URLs")
    return SearchRun(documents, log, error)
