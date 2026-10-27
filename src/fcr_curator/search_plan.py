"""Decide which SerpAPI searches a run of the search task makes (run by dbt's fcr_search_plan model).

The plan allows 1,000 searches a month, and even an empty results page costs one, so every run asks
only the queries most likely to find an FCR PDF we don't have yet. In priority order:

1. missing_year  - site: query for a domain with an FCR in the register, but not for the latest year
2. institution   - name query for an insurer on the entity list with no FCR for the latest year, under
                   its name and each alias, without legal suffixes such as Ltd.: a report says "Arch
                   Reinsurance", not always "Arch Reinsurance Ltd."
3. discovery     - generic queries per reporting year; the only way to find new websites

A query that ran recently is skipped: for 28 days, or 90 days if it found nothing FCR-like last time.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from fcr_curator.curation import EXCLUDED_DOMAINS, institution_key

# Added to open-ended queries so pages from excluded sites don't use up paid result slots
EXCLUDED_SITES = " ".join(f"-site:{domain}" for domain in EXCLUDED_DOMAINS)

# Trailing legal words a report's text often leaves out: "Partner Reinsurance Company Ltd." -> "Partner Reinsurance"
TRAILING_LEGAL_WORDS = re.compile(
    r"(?:[\s,]+(?:ltd|limited|inc|plc|llc|s\.?a|se|ag|n\.?v|company|co)\.?)+$", re.IGNORECASE
)

# Some Bermuda insurers call their FCR a "Solvency and Financial Condition Report", so both names are searched
DISCOVERY_TEMPLATES = (
    f'"financial condition report" Bermuda {{year}} filetype:pdf {EXCLUDED_SITES}',
    f'"financial condition report" "Bermuda Monetary Authority" {{year}} filetype:pdf {EXCLUDED_SITES}',
    f'"solvency and financial condition report" Bermuda {{year}} filetype:pdf {EXCLUDED_SITES}',
)


@dataclass(frozen=True)
class SearchSettings:
    """What a run looks for, and how long it waits before repeating a query."""

    latest_year: int  # Newest reporting year to look for
    # Insurers to search for by name, in search order: each one's names, its registered name first,
    # then its aliases
    institutions: tuple[tuple[str, ...], ...] = ()
    discovery_years: int = 3  # Reporting years the generic queries cover, counting back from latest_year
    requery_after_days: int = 28
    dead_query_requery_days: int = 90  # For a query whose last run found nothing FCR-like


@dataclass(frozen=True)
class PlannedQuery:
    """One query to ask, why, and the most result pages it may use."""

    query: str
    reason: str  # missing_year, institution or discovery
    max_pages: int


@dataclass(frozen=True)
class QueryHistory:
    """When a query last ran, and how many FCR-like results that run found."""

    last_searched_at: datetime
    fcr_results: int


@dataclass(frozen=True)
class Coverage:
    """Reporting years the register already has an FCR for, by domain and by institution."""

    domains: Mapping[str, set[int]]
    institutions: Mapping[str, set[int]]  # Keyed by the insurer's registered name


# --- Query text -------------------------------------------------------------------------------


def site_query(domain: str, year: int) -> str:
    """Query one website for its FCR for one year."""
    return f'site:{domain} "financial condition report" {year} filetype:pdf'


def search_name(name: str) -> str:
    """Return a name as reports write it, without trailing legal words: 'Hiscox Ltd' -> 'Hiscox'."""
    return TRAILING_LEGAL_WORDS.sub("", name).strip() or name


def institution_query(name: str) -> str:
    """Query one insurer's FCRs by name."""
    return f'"{search_name(name)}" "financial condition report" filetype:pdf {EXCLUDED_SITES}'


# --- What we already have ---------------------------------------------------------------------


def coverage(register_rows: Iterable[dict]) -> Coverage:
    """Collect which reporting years the register holds, per domain and per insurer's registered name."""
    domains: dict[str, set[int]] = defaultdict(set)
    institutions: dict[str, set[int]] = defaultdict(set)
    for row in register_rows:
        year = row.get("reporting_year")
        if not year:
            continue
        if row.get("domain"):
            domains[row["domain"]].add(year)
        if row.get("registered_name"):
            institutions[row["registered_name"]].add(year)
    return Coverage(dict(domains), dict(institutions))


def query_history(log_rows: Iterable[dict]) -> dict[str, QueryHistory]:
    """Summarise the search log into each query's latest run.

    One run of a query can span several log rows (one per page); they share a searched_at, so their
    FCR-like results are added up.
    """
    rows = list(log_rows)
    latest = {}
    for row in rows:
        latest[row["query"]] = max(latest.get(row["query"], ""), row["searched_at"])

    found: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["searched_at"] == latest[row["query"]]:
            found[row["query"]] += row["fcr_results"] or 0

    return {query: QueryHistory(datetime.fromisoformat(at), found[query]) for query, at in latest.items()}


# --- The plan ---------------------------------------------------------------------------------


def is_due(history: QueryHistory | None, now: datetime, settings: SearchSettings) -> bool:
    """Return True if a query never ran, or its waiting period has passed."""
    if history is None:
        return True
    wait = settings.dead_query_requery_days if history.fcr_results == 0 else settings.requery_after_days
    return now - history.last_searched_at >= timedelta(days=wait)


def _has_latest_fcr(name: str, known: Coverage, year: int) -> bool:
    # Exact: "Everest Group Ltd." isn't covered by "Everest Reinsurance (Bermuda), Ltd."'s FCR
    key = institution_key(name)
    return any(year in years and institution_key(known_name) == key for known_name, years in known.institutions.items())


def plan_queries(
    settings: SearchSettings, known: Coverage, history: Mapping[str, QueryHistory], now: datetime
) -> list[PlannedQuery]:
    """Return the queries due this run, most promising first.

    The plan isn't trimmed to a budget here; the search stops when the run's budget is spent, so the
    most promising queries are the ones that get asked.
    """
    year = settings.latest_year
    discovery_years = range(year, year - settings.discovery_years, -1)  # Newest first

    candidates = [
        *(
            PlannedQuery(site_query(domain, year), "missing_year", max_pages=1)
            for domain, years in sorted(known.domains.items())
            if year not in years
        ),
        *(
            PlannedQuery(institution_query(name), "institution", max_pages=1)
            for names in settings.institutions
            if not _has_latest_fcr(names[0], known, year)
            for name in names
        ),
        *(
            PlannedQuery(template.format(year=discovery_year), "discovery", max_pages=2)
            for discovery_year in discovery_years
            for template in DISCOVERY_TEMPLATES
        ),
    ]

    plan, seen = [], set()
    for planned in candidates:
        if planned.query not in seen and is_due(history.get(planned.query), now, settings):
            seen.add(planned.query)
            plan.append(planned)
    return plan


def worst_case_searches(plan: Iterable[PlannedQuery]) -> int:
    """Return the most searches a plan can use: every page it's allowed."""
    return sum(planned.max_pages for planned in plan)


def search_budget(searches_left: int, run_cap: int, reserve: int) -> int:
    """Return the searches this run may use: at most run_cap, never dipping into the reserve."""
    return max(0, min(run_cap, searches_left - reserve))
