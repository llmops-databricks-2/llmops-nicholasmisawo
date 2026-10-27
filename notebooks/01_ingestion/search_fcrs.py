# Databricks notebook source
# MAGIC %md
# MAGIC # Search for FCRs: SerpAPI, and the disclosures pages on the reference list
# MAGIC
# MAGIC dbt plans the searches first (`fcr_search_plan`, from `search_plan.py`): only queries likely to find
# MAGIC an FCR we don't have yet, most promising first. This does what dbt can't:
# MAGIC
# MAGIC 1. Check the budget: searches left this month, minus a reserve, capped per run
# MAGIC 2. Ask the plan's queries in order until the budget is spent
# MAGIC 3. Read the reference list's `fcr_pages` for their PDF links (`sources/pages.py`; costs no searches)
# MAGIC 4. Append what happened: every PDF found (`raw_fcr_search_hits`), every search made
# MAGIC    (`raw_fcr_searches`) and every page read (`raw_fcr_page_reads`). dbt works out the current
# MAGIC    search results from them, delisting included (`dbt/models/intermediate/int_fcr_search_results.sql`)
# MAGIC
# MAGIC To see the plan and its cost without spending a search, run the job with
# MAGIC `--only dbt_search_plan,search_fcrs --params dry_run=true`: it stops after step 1, changing nothing.

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession

from fcr_curator.config import load_config
from fcr_curator.http_client import create_session
from fcr_curator.landing import append_facts
from fcr_curator.reference import REFERENCE_TABLE, entities_from_rows
from fcr_curator.search_plan import PlannedQuery, search_budget, worst_case_searches
from fcr_curator.sources.pages import fetch_page_documents
from fcr_curator.sources.serpapi import SearchRun, fetch_fcr_documents, searches_left
from fcr_curator.tables import PAGE_READS_TABLE, SEARCH_HITS_TABLE, SEARCH_PLAN_TABLE, SEARCHES_TABLE

# COMMAND ----------
# Settings, and the plan dbt built for this run

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
dbutils.widgets.text("dry_run", "false")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
DRY_RUN = dbutils.widgets.get("dry_run").strip().lower() == "true"

RUN_SEARCH_CAP = 150  # Weekly runs at this cap use at most ~650 of the 1,000 monthly searches
SEARCH_RESERVE = 100  # Always left for manual and dev runs

plan = [
    PlannedQuery(row.query, row.reason, row.max_pages)
    for row in spark.table(f"{cfg.full_schema_name}.{SEARCH_PLAN_TABLE}").orderBy("priority").collect()
]
# The disclosures pages to read are the reference list's fcr_pages (the dbt seed ref_bermuda_entities)
REFERENCE_ENTITIES = entities_from_rows(
    row.asDict() for row in spark.table(f"{cfg.full_schema_name}.{REFERENCE_TABLE}").collect()
)

logger.info(f"{len(plan)} queries due, using at most {worst_case_searches(plan)} searches:")
for planned in plan[:20]:
    logger.info(f"  [{planned.reason}] {planned.query}")

# COMMAND ----------
# 1. Check the budget (the account check is free)

api_key = dbutils.secrets.get(scope=cfg.serpapi_secret_scope, key=cfg.serpapi_secret_key)
with create_session() as session:
    left = searches_left(session, api_key)
budget = search_budget(left, RUN_SEARCH_CAP, SEARCH_RESERVE)
logger.info(f"Searches left this month: {left}; this run may use: {budget}")

if DRY_RUN:
    pages = sum(len(entity.fcr_pages) for entity in REFERENCE_ENTITIES)
    dbutils.notebook.exit(f"Dry run: {len(plan)} queries due, budget {budget}, {pages} pages to read; nothing changed")

# COMMAND ----------
# 2. Search (when anything is due and affordable)

run = SearchRun(documents=[], log=[], error=None)
if plan and budget:
    with create_session() as session:
        run = fetch_fcr_documents(session, api_key, plan, max_searches=budget)
logger.info(f"Used {len(run.log)} searches; found {len(run.documents)} distinct results")

# COMMAND ----------
# 3. Read the disclosures pages (every run: it costs no searches, and catches newly posted FCRs)

with create_session() as session:
    page_read = fetch_page_documents(session, REFERENCE_ENTITIES)
logger.info(f"Pages read: {len(page_read.pages_read)}; PDF links found: {len(page_read.documents)}")
for error in page_read.errors:
    logger.warning(f"Page not read, will retry next run: {error}")

# COMMAND ----------
# 4. Append the run. A page that loaded counts as asked, so dbt delists a PDF it no longer links like a
# search result no longer returned.

append_facts(
    spark,
    cfg.full_schema_name,
    {
        SEARCH_HITS_TABLE: [*run.documents, *page_read.documents],
        SEARCHES_TABLE: run.log,
        PAGE_READS_TABLE: page_read.pages_read,
    },
)

# A SerpAPI failure fails the task, but only after landing what the run already paid for
if run.error:
    raise RuntimeError(f"Search stopped early: {run.error}")
