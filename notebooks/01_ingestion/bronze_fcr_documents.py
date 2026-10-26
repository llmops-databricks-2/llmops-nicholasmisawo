# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze: find FCR PDFs with SerpAPI
# MAGIC
# MAGIC 1. Plan the searches: only queries likely to find an FCR we don't have yet (`search_plan.py`)
# MAGIC 2. Check the budget: searches left this month, minus a reserve, capped per run
# MAGIC 3. Search, and log every search made
# MAGIC 4. Merge the results into `bronze_fcr_documents`
# MAGIC
# MAGIC Run the job with `--params dry_run=true` to stop after step 2 without using any searches.

# COMMAND ----------

from datetime import UTC, date, datetime

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

from fcr_curator.config import load_config
from fcr_curator.http_client import create_session
from fcr_curator.schemas import BRONZE_SCHEMA, SEARCH_LOG_SCHEMA, SOURCE_SCHEMA
from fcr_curator.search_plan import (
    SearchSettings,
    coverage,
    plan_queries,
    query_history,
    search_budget,
    worst_case_searches,
)
from fcr_curator.sources.serpapi import fetch_fcr_documents, searches_left
from fcr_curator.tables import bronze_merge_sql, delist_sql, delisted_ids, ensure_table

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
dbutils.widgets.text("dry_run", "false")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
DRY_RUN = dbutils.widgets.get("dry_run").strip().lower() == "true"

SETTINGS = SearchSettings(
    latest_year=date.today().year - 1,  # FCRs are published after the year ends
    institutions=(),  # Insurers to search for by name, e.g. ("Arch Reinsurance Ltd.",)
)
RUN_SEARCH_CAP = 150  # Weekly runs at this cap use at most ~650 of the 1,000 monthly searches
SEARCH_RESERVE = 100  # Always left for manual and dev runs

BRONZE_TABLE = f"{cfg.full_schema_name}.bronze_fcr_documents"
SEARCH_LOG_TABLE = f"{cfg.full_schema_name}.bronze_fcr_search_log"
SILVER_TABLE = f"{cfg.full_schema_name}.silver_fcr_documents"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.full_schema_name}")
ensure_table(spark, BRONZE_TABLE, BRONZE_SCHEMA, "bronze", "Bronze: every FCR search result SerpAPI has returned.")
ensure_table(spark, SEARCH_LOG_TABLE, SEARCH_LOG_SCHEMA, "bronze", "Bronze: one row per SerpAPI search made.")

# COMMAND ----------
# 1. Plan the searches: skip what silver already has and what was searched recently

silver_rows = (
    [row.asDict() for row in spark.table(SILVER_TABLE).select("domain", "institution", "reporting_year").collect()]
    if spark.catalog.tableExists(SILVER_TABLE)
    else []
)
history = query_history(row.asDict() for row in spark.table(SEARCH_LOG_TABLE).collect())
plan = plan_queries(SETTINGS, coverage(silver_rows), history, now=datetime.now(UTC))

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
    dbutils.notebook.exit(f"Dry run: {len(plan)} queries due, budget {budget}; nothing searched")
if not plan or budget == 0:
    dbutils.notebook.exit(f"Nothing searched: {len(plan)} queries due, budget {budget}")

# COMMAND ----------
# 3. Search, then log every search straight away so it's never repeated too soon

with create_session() as session:
    run = fetch_fcr_documents(session, api_key, plan, max_searches=budget)

if run.log:
    spark.createDataFrame(run.log, schema=SEARCH_LOG_SCHEMA).write.mode("append").saveAsTable(SEARCH_LOG_TABLE)
logger.info(f"Used {len(run.log)} searches; found {len(run.documents)} distinct results")

# COMMAND ----------
# 4. Merge into bronze, then mark results that are no longer found (see tables.delisted_ids)

if run.documents:
    spark.createDataFrame(run.documents, schema=SOURCE_SCHEMA).createOrReplaceTempView("search_results")
    spark.sql(bronze_merge_sql(BRONZE_TABLE, "search_results", SOURCE_SCHEMA.fieldNames())).show()

listed = spark.table(BRONZE_TABLE).where(col("is_listed")).select("document_id", "search_queries").collect()
to_delist = delisted_ids(
    [row.asDict() for row in listed],
    asked_queries={entry["query"] for entry in run.log},
    returned_ids={document["document_id"] for document in run.documents},
)
if to_delist:
    spark.createDataFrame([(i,) for i in to_delist], "document_id string").createOrReplaceTempView("delisted")
    spark.sql(delist_sql(BRONZE_TABLE, "delisted"))
logger.info(f"Marked {len(to_delist)} results as no longer found")

# A SerpAPI failure fails the task, but only after saving what the run already paid for
if run.error:
    raise RuntimeError(f"Search stopped early: {run.error}")

# COMMAND ----------
# Summary

spark.table(BRONZE_TABLE).groupBy("domain").count().orderBy(col("count").desc()).show(20, truncate=False)
spark.table(SEARCH_LOG_TABLE).groupBy("reason").sum("fcr_results").show()
