"""Plan which SerpAPI searches a run spends its budget on.

Searches are the scarce resource: the plan allows a fixed number a month, and a page with no results
still counts as one. So each run asks only the queries likely to find an FCR PDF silver doesn't hold
yet, most promising first:

1. ``missing_year``: a site: query for each domain that has published an FCR before but not one for
   the latest reporting year. One page, and the hit rate is high because the domain is known.
2. ``institution``: a query for each named institution with no FCR for the latest reporting year.
3. ``discovery``: generic per-year queries, the only ones that find domains not seen before.

A query isn't asked again until REQUERY_AFTER_DAYS have passed since it last ran, or
DEAD_QUERY_REQUERY_DAYS when that run found nothing that looked like an FCR. The plan isn't cut to a
budget here: fetch_fcr_documents stops at the run's search cap, so the most promising queries run first.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from fcr_curator.curation import dedupe_key

REQUERY_AFTER_DAYS = 28
DEAD_QUERY_REQUERY_DAYS = 90

# Generic queries, asked once per reporting year
DISCOVERY_TEMPLATES = (
    '"financial condition report" Bermuda {year} filetype:pdf',
    '"financial condition report" "Bermuda Monetary Authority" {year} filetype:pdf',
)
DISCOVERY_PAGES = 2
TARGETED_PAGES = 1  # A known domain or a named institution rarely needs a second page


@dataclass(frozen=True)
class PlannedQuery:
    """One query a run may ask, why it's asked, and the most pages it may cost."""

    query: str
    reason: str  # missing_year, institution or discovery
    max_pages: int


@dataclass(frozen=True)
class QueryHistory:
    """When a query last ran, and how many FCR-looking results that run found across its pages."""

    last_searched_at: datetime
    fcr_results: int


@dataclass(frozen=True)
class Coverage:
    """Reporting years silver already holds an FCR for, by domain and by institution name key."""

    domains: Mapping[str, set[int]]
    institutions: Mapping[str, set[int]]


def site_query(domain: str, year: int) -> str:
    """Query one domain for its FCR for one reporting year."""
    return f'site:{domain} "financial condition report" {year} filetype:pdf'


def institution_query(name: str) -> str:
    """Query one institution's FCRs by name."""
    return f'"{name}" "financial condition report" filetype:pdf'


def coverage(silver_rows: Iterable[dict]) -> Coverage:
    """Collect the reporting years silver holds per domain and per institution."""
    domains: dict[str, set[int]] = {}
    institutions: dict[str, set[int]] = {}
    for row in silver_rows:
        year = row.get("reporting_year")
        if not year:
            continue
        if row.get("domain"):
            domains.setdefault(row["domain"], set()).add(year)
        if row.get("institution"):
            institutions.setdefault(dedupe_key(row["institution"]), set()).add(year)
    return Coverage(domains, institutions)


def query_history(log_rows: Iterable[dict]) -> dict[str, QueryHistory]:
    """Summarise the search log: each query's latest run and the FCR-looking results it found.

    A run spans several log rows (one per page), all sharing its ``searched_at``.
    """
    latest: dict[str, tuple[str, int]] = {}
    for row in log_rows:
        query, searched_at, found = row["query"], row["searched_at"], row["fcr_results"] or 0
        previous = latest.get(query)
        if previous is None or searched_at > previous[0]:
            latest[query] = (searched_at, found)
        elif searched_at == previous[0]:
            latest[query] = (searched_at, previous[1] + found)
    return {query: QueryHistory(datetime.fromisoformat(at), found) for query, (at, found) in latest.items()}


def is_due(
    history: QueryHistory | None,
    now: datetime,
    requery_after_days: int = REQUERY_AFTER_DAYS,
    dead_query_requery_days: int = DEAD_QUERY_REQUERY_DAYS,
) -> bool:
    """Return whether a query should be asked again: never asked, or its wait has passed."""
    if history is None:
        return True
    wait = dead_query_requery_days if history.fcr_results == 0 else requery_after_days
    return now - history.last_searched_at >= timedelta(days=wait)


def _institution_covered(name: str, covered: Mapping[str, set[int]], year: int) -> bool:
    # Silver's institution comes from a title, so "Example Re" should match "Example Re Ltd."
    key = dedupe_key(name)
    return any(year in years and (key in silver_key or silver_key in key) for silver_key, years in covered.items())


def plan_queries(
    latest_year: int,
    discovery_years: Iterable[int],
    institutions: Iterable[str],
    known: Coverage,
    history: Mapping[str, QueryHistory],
    now: datetime,
    requery_after_days: int = REQUERY_AFTER_DAYS,
    dead_query_requery_days: int = DEAD_QUERY_REQUERY_DAYS,
) -> list[PlannedQuery]:
    """Return the queries due this run, most likely to find a new FCR PDF first, without duplicates."""
    candidates = [
        *(
            PlannedQuery(site_query(domain, latest_year), "missing_year", TARGETED_PAGES)
            for domain, years in sorted(known.domains.items())
            if latest_year not in years
        ),
        *(
            PlannedQuery(institution_query(name), "institution", TARGETED_PAGES)
            for name in institutions
            if not _institution_covered(name, known.institutions, latest_year)
        ),
        *(
            PlannedQuery(template.format(year=year), "discovery", DISCOVERY_PAGES)
            for year in sorted(discovery_years, reverse=True)
            for template in DISCOVERY_TEMPLATES
        ),
    ]

    planned: dict[str, PlannedQuery] = {}
    for candidate in candidates:
        due = is_due(history.get(candidate.query), now, requery_after_days, dead_query_requery_days)
        if due and candidate.query not in planned:
            planned[candidate.query] = candidate
    return list(planned.values())


def worst_case_searches(plan: Iterable[PlannedQuery]) -> int:
    """Return the most searches a plan can cost, if every page it may ask for comes back full."""
    return sum(query.max_pages for query in plan)
