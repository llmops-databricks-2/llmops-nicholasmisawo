"""Plan this run's SerpAPI searches with the pipeline's own rules (fcr_curator.search_plan).

Python, because planning is those rules: a site: query for each domain in the register missing the
latest year, a name query for each reference-list entity without it, and the generic discovery
queries, each skipped while it waits after its last run (28 days, or 90 if it found nothing FCR-like).
Rebuilt at the start of every pipeline run; the search task then asks the queries in priority order
until its budget is spent, so the most promising ones are always asked first.
"""

from datetime import UTC, date, datetime

from pyspark.sql import DataFrame, SparkSession

PLAN_SCHEMA = "priority int, query string, reason string, max_pages int"


def model(dbt: object, session: SparkSession) -> DataFrame:
    """Return one row per query due this run, most promising first."""
    from fcr_curator.reference import entities_from_rows  # From the pipeline's wheel
    from fcr_curator.search_plan import SearchSettings, coverage, plan_queries, query_history

    dbt.config(materialized="table")
    # Each ref on its own line: dbt finds a model's refs by reading its code, and misses one in a generator
    reference_list = dbt.ref("bermuda_entities")
    register = dbt.ref("int_fcr_register")
    searches = dbt.ref("stg_fcr_searches")

    entities = entities_from_rows(row.asDict() for row in reference_list.collect())
    settings = SearchSettings(
        latest_year=date.today().year - 1,  # FCRs are published after the year ends
        institutions=tuple(name for entity in entities for name in entity.names),
    )
    known = coverage(row.asDict() for row in register.select("domain", "institution", "reporting_year").collect())
    history = query_history(row.asDict() for row in searches.select("query", "searched_at", "fcr_results").collect())

    plan = plan_queries(settings, known, history, now=datetime.now(UTC))
    rows = [(priority, planned.query, planned.reason, planned.max_pages) for priority, planned in enumerate(plan, 1)]
    return session.createDataFrame(rows, schema=PLAN_SCHEMA)
