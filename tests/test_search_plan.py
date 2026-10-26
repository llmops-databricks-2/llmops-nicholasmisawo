"""Tests for planning which SerpAPI searches a run spends its budget on."""

from datetime import UTC, datetime, timedelta

from fcr_curator.search_plan import (
    Coverage,
    QueryHistory,
    SearchSettings,
    coverage,
    institution_query,
    is_due,
    plan_queries,
    query_history,
    search_budget,
    site_query,
    worst_case_searches,
)

NOW = datetime(2026, 10, 26, tzinfo=UTC)
NOTHING_KNOWN = Coverage({}, {})
SETTINGS = SearchSettings(latest_year=2025, discovery_years=2)


def plan(known: Coverage = NOTHING_KNOWN, institutions: tuple[str, ...] = (), history: dict | None = None) -> list:
    """Plan for latest year 2025, with discovery over 2025 and 2024."""
    settings = SearchSettings(latest_year=2025, institutions=institutions, discovery_years=2)
    return plan_queries(settings, known, history or {}, now=NOW)


def test_plan_asks_known_domains_missing_the_latest_year_first() -> None:
    """A domain that published before but not for the latest year gets a one-page site: query, ahead of discovery."""
    known = Coverage({"example-re.com": {2023, 2024}, "done.com": {2025}}, {})

    planned = plan(known)

    assert planned[0].query == site_query("example-re.com", 2025)
    assert planned[0].max_pages == 1
    assert all("done.com" not in query.query for query in planned)


def test_plan_skips_institutions_silver_already_holds_for_the_latest_year() -> None:
    """A named institution is searched only until silver has its latest FCR, matched loosely by name."""
    known = Coverage({}, {"Example Re Ltd.": {2025}})

    queries = [query.query for query in plan(known, institutions=("Example Re", "Other Insurance Ltd."))]

    assert institution_query("Example Re") not in queries
    assert institution_query("Other Insurance Ltd.") in queries


def test_plan_runs_discovery_newest_year_first_and_last() -> None:
    """Discovery comes after targeted queries, newest reporting year first."""
    planned = plan(Coverage({"example-re.com": {2024}}, {}))

    reasons = [query.reason for query in planned]
    assert reasons == ["missing_year"] + ["discovery"] * 6
    assert "2025" in planned[1].query


def test_plan_skips_queries_asked_recently() -> None:
    """A query asked within the requery window isn't asked again."""
    first = plan()
    history = {first[0].query: QueryHistory(NOW - timedelta(days=3), fcr_results=5)}

    assert first[0].query not in [query.query for query in plan(history=history)]


def test_is_due_waits_longer_for_a_query_that_found_nothing() -> None:
    """A query whose last run found no FCRs waits the longer dead-query window."""
    forty_days_ago = NOW - timedelta(days=40)

    assert is_due(QueryHistory(forty_days_ago, fcr_results=2), NOW, SETTINGS)
    assert not is_due(QueryHistory(forty_days_ago, fcr_results=0), NOW, SETTINGS)
    assert is_due(None, NOW, SETTINGS)


def test_query_history_sums_the_pages_of_each_querys_latest_run() -> None:
    """The latest run's pages add up; an older run of the same query is ignored."""
    log = [
        {"query": "q", "searched_at": "2026-09-01T00:00:00+00:00", "fcr_results": 9},
        {"query": "q", "searched_at": "2026-10-01T00:00:00+00:00", "fcr_results": 2},
        {"query": "q", "searched_at": "2026-10-01T00:00:00+00:00", "fcr_results": 0},
    ]

    assert query_history(log)["q"] == QueryHistory(datetime(2026, 10, 1, tzinfo=UTC), fcr_results=2)


def test_coverage_collects_years_by_domain_and_institution() -> None:
    """Silver rows with a reporting year count towards their domain and institution; undated ones don't."""
    known = coverage(
        [
            {"domain": "example-re.com", "institution": "Example Re Ltd.", "reporting_year": 2024},
            {"domain": "example-re.com", "institution": "Example Re Ltd.", "reporting_year": None},
        ]
    )

    assert known.domains == {"example-re.com": {2024}}
    assert known.institutions == {"Example Re Ltd.": {2024}}


def test_worst_case_searches_counts_every_page_a_plan_may_ask_for() -> None:
    """The dry-run cost is the sum of each query's page allowance."""
    assert worst_case_searches(plan(Coverage({"example-re.com": {2024}}, {}))) == 1 + 6 * 2


def test_open_ended_queries_exclude_the_bma_website() -> None:
    """Discovery and institution queries leave out bma.bm and .gov sites, so they don't use paid slots."""
    queries = [query.query for query in plan(institutions=("Example Re",))]

    assert queries
    assert all(query.endswith("-site:bma.bm -site:gov") for query in queries)


def test_search_budget_never_exceeds_the_run_cap_or_touches_the_reserve() -> None:
    """A run spends at most its cap, keeps the reserve untouched, and never goes negative."""
    assert search_budget(searches_left=900, run_cap=150, reserve=100) == 150
    assert search_budget(searches_left=180, run_cap=150, reserve=100) == 80
    assert search_budget(searches_left=50, run_cap=150, reserve=100) == 0


def test_discovery_searches_both_names_bermuda_insurers_use() -> None:
    """Each discovery year asks for "financial condition report" and "solvency and financial condition report"."""
    queries = [query.query for query in plan() if "2025" in query.query]

    assert any(query.startswith('"financial condition report"') for query in queries)
    assert any(query.startswith('"solvency and financial condition report"') for query in queries)


def test_a_generic_institution_name_in_silver_covers_no_one() -> None:
    """A silver row named just "Group" mustn't make every "... Group" insurer look covered and go unsearched."""
    known = Coverage({}, {"Group": {2025}})

    queries = [query.query for query in plan(known, institutions=("Arch Capital Group", "Enstar Group"))]

    assert institution_query("Arch Capital Group") in queries
    assert institution_query("Enstar Group") in queries
