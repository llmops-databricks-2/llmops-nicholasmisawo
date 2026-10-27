# Databricks notebook source
# MAGIC %md
# MAGIC # Search for FCRs: SerpAPI, and the disclosures pages on the reference list
# MAGIC
# MAGIC 1. Plan the searches: only queries likely to find an FCR we don't have yet (`search_plan.py`)
# MAGIC 2. Check the budget: searches left this month, minus a reserve, capped per run
# MAGIC 3. Search, and log every search made
# MAGIC 4. Read the reference list's `fcr_pages` for their PDF links (`sources/pages.py`; costs no searches)
# MAGIC 5. Merge everything found into `raw_fcr_search_results`, which dbt reads as a source
# MAGIC
# MAGIC Run the job with `--params dry_run=true` to stop after step 2, changing nothing.

# COMMAND ----------

from datetime import UTC, date, datetime

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

from fcr_curator.config import load_config
from fcr_curator.http_client import create_session
from fcr_curator.reference import REFERENCE_TABLE, entities_from_rows
from fcr_curator.schemas import SEARCH_LOG_SCHEMA, SEARCH_RESULTS_SCHEMA, SOURCE_SCHEMA
from fcr_curator.search_plan import (
    SearchSettings,
    coverage,
    plan_queries,
    query_history,
    search_budget,
    worst_case_searches,
)
from fcr_curator.sources.pages import fetch_page_documents
from fcr_curator.sources.serpapi import SearchRun, fetch_fcr_documents, searches_left
from fcr_curator.tables import (
    REGISTER_TABLE,
    SEARCH_LOG_TABLE,
    SEARCH_RESULTS_TABLE,
    delist_sql,
    delisted_ids,
    ensure_table,
    search_results_merge_sql,
)

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
dbutils.widgets.text("dry_run", "false")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
DRY_RUN = dbutils.widgets.get("dry_run").strip().lower() == "true"

# (Re)insurers searched for by name, largest assets first (the dbt seed ref_bermuda_entities), each under
# its current and previous names; a name is skipped once the register holds its FCR for the latest year
REFERENCE_TABLE_NAME = f"{cfg.full_schema_name}.{REFERENCE_TABLE}"
if not spark.catalog.tableExists(REFERENCE_TABLE_NAME):
    raise RuntimeError(f"{REFERENCE_TABLE_NAME} not found; run the dbt_seed task first")
REFERENCE_ENTITIES = entities_from_rows(row.asDict() for row in spark.table(REFERENCE_TABLE_NAME).collect())
SETTINGS = SearchSettings(
    latest_year=date.today().year - 1,  # FCRs are published after the year ends
    institutions=tuple(name for entity in REFERENCE_ENTITIES for name in entity.names),
)
RUN_SEARCH_CAP = 150  # Weekly runs at this cap use at most ~650 of the 1,000 monthly searches
SEARCH_RESERVE = 100  # Always left for manual and dev runs

RESULTS = f"{cfg.full_schema_name}.{SEARCH_RESULTS_TABLE}"
SEARCH_LOG = f"{cfg.full_schema_name}.{SEARCH_LOG_TABLE}"
REGISTER = f"{cfg.full_schema_name}.{REGISTER_TABLE}"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.full_schema_name}")
ensure_table(
    spark, RESULTS, SEARCH_RESULTS_SCHEMA, "raw", "Raw: every FCR PDF found by search or on a disclosures page."
)
ensure_table(spark, SEARCH_LOG, SEARCH_LOG_SCHEMA, "raw", "Raw: one row per SerpAPI search made.")

# COMMAND ----------
# 1. Plan the searches: skip what the register already has and what was searched recently

register_rows = (
    [row.asDict() for row in spark.table(REGISTER).select("domain", "institution", "reporting_year").collect()]
    if spark.catalog.tableExists(REGISTER)
    else []
)
history = query_history(row.asDict() for row in spark.table(SEARCH_LOG).collect())
plan = plan_queries(SETTINGS, coverage(register_rows), history, now=datetime.now(UTC))

logger.info(f"{len(plan)} queries due, using at most {worst_case_searches(plan)} searches:")
for planned in plan[:20]:
    logger.info(f"  [{planned.reason}] {planned.query}")

# COMMAND ----------
# 2. Check the budget (the account check is free)

api_key = dbutils.secrets.get(scope=cfg.serpapi_secret_scope, key=cfg.serpapi_secret_key)
with create_session() as session:
    left = searches_left(session, api_key)
budget = search_budget(left, RUN_SEARCH_CAP, SEARCH_RESERVE)
logger.info(f"Searches left this month: {left}; this run may use: {budget}")

if DRY_RUN:
    pages = sum(len(entity.fcr_pages) for entity in REFERENCE_ENTITIES)
    dbutils.notebook.exit(f"Dry run: {len(plan)} queries due, budget {budget}, {pages} pages to read; nothing changed")

# COMMAND ----------
# 3. Search (when anything is due and affordable), then log every search so it's never repeated too soon

run = SearchRun(documents=[], log=[], error=None)
if plan and budget:
    with create_session() as session:
        run = fetch_fcr_documents(session, api_key, plan, max_searches=budget)

if run.log:
    spark.createDataFrame(run.log, schema=SEARCH_LOG_SCHEMA).write.mode("append").saveAsTable(SEARCH_LOG)
logger.info(f"Used {len(run.log)} searches; found {len(run.documents)} distinct results")

# COMMAND ----------
# 4. Read the disclosures pages (every run: it costs no searches, and catches newly posted FCRs)

with create_session() as session:
    page_read = fetch_page_documents(session, REFERENCE_ENTITIES)
logger.info(f"Pages read: {len(page_read.pages_read)}; PDF links found: {len(page_read.documents)}")
for error in page_read.errors:
    logger.warning(f"Page not read, will retry next run: {error}")

# COMMAND ----------
# 5. Merge into the search results, then mark results that are no longer found (see tables.delisted_ids). A page that
# loaded counts as asked, so a PDF it no longer links is delisted like a search result no longer returned.

found = [*run.documents, *page_read.documents]
if found:
    spark.createDataFrame(found, schema=SOURCE_SCHEMA).createOrReplaceTempView("found_documents")
    spark.sql(search_results_merge_sql(RESULTS, "found_documents", SOURCE_SCHEMA.fieldNames())).show()

listed = spark.table(RESULTS).where(col("is_listed")).select("document_id", "search_queries").collect()
to_delist = delisted_ids(
    [row.asDict() for row in listed],
    asked_queries={entry["query"] for entry in run.log} | set(page_read.pages_read),
    returned_ids={document["document_id"] for document in found},
)
if to_delist:
    spark.createDataFrame([(i,) for i in to_delist], "document_id string").createOrReplaceTempView("delisted")
    spark.sql(delist_sql(RESULTS, "delisted"))
logger.info(f"Marked {len(to_delist)} results as no longer found")

# A SerpAPI failure fails the task, but only after saving what the run already paid for
if run.error:
    raise RuntimeError(f"Search stopped early: {run.error}")

# COMMAND ----------
# Summary

spark.table(RESULTS).groupBy("domain").count().orderBy(col("count").desc()).show(20, truncate=False)
spark.table(SEARCH_LOG).groupBy("reason").sum("fcr_results").show()
