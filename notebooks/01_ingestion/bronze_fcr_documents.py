# Databricks notebook source

from datetime import UTC, date, datetime

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count, explode, size, when
from pyspark.sql.functions import sum as spark_sum

from fcr_curator.config import load_config
from fcr_curator.http_client import create_session
from fcr_curator.schemas import BRONZE_SCHEMA, SEARCH_LOG_SCHEMA, SOURCE_SCHEMA
from fcr_curator.search_plan import coverage, plan_queries, query_history, worst_case_searches
from fcr_curator.sources.serpapi import fetch_fcr_documents, searches_left
from fcr_curator.tables import bronze_merge_sql, delist_sql, delisted_ids, ensure_table

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
dbutils.widgets.text("dry_run", "false")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.bronze_fcr_documents"
SEARCH_LOG_TABLE = f"{CATALOG}.{SCHEMA}.bronze_fcr_search_log"
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.silver_fcr_documents"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
ensure_table(
    spark,
    BRONZE_TABLE,
    BRONZE_SCHEMA,
    layer="bronze",
    comment="Bronze: FCR search results as SerpAPI returned them, upserted on every ingest run.",
)
ensure_table(
    spark,
    SEARCH_LOG_TABLE,
    SEARCH_LOG_SCHEMA,
    layer="bronze",
    comment="Bronze: one row per billed SerpAPI search, appended by every ingest run.",
)
logger.info(f"Tables {BRONZE_TABLE} and {SEARCH_LOG_TABLE} ready")

# COMMAND ----------
# Plan the Searches
# Searches are billed against a fixed monthly plan, and a page with no results still counts, so the run
# asks only queries likely to find an FCR PDF silver doesn't hold yet (see search_plan.py): site: queries
# for known domains missing the latest year first, then named institutions, then generic discovery. A
# query isn't asked again for REQUERY_AFTER_DAYS, or DEAD_QUERY_REQUERY_DAYS when it last found nothing.

# FCRs are filed after the year ends, so the latest reporting year is last year
LATEST_REPORTING_YEAR = date.today().year - 1
DISCOVERY_YEARS = range(LATEST_REPORTING_YEAR - 2, LATEST_REPORTING_YEAR + 1)
# Institutions to search for by name; skipped once silver holds their latest FCR. e.g. ["Arch Reinsurance Ltd."]
INSTITUTIONS: list[str] = []
REQUERY_AFTER_DAYS = 28
DEAD_QUERY_REQUERY_DAYS = 90

# Weekly runs at this cap spend at most about 4.3 * RUN_SEARCH_CAP a month, under a 1,000-search plan
RUN_SEARCH_CAP = 150
# Left in the plan for manual and dev runs; the account check covers every environment sharing the key
SEARCH_RESERVE = 100
# The job's dry_run parameter: true prints the plan and its cost without searching
DRY_RUN = dbutils.widgets.get("dry_run").strip().lower() == "true"

silver_rows = (
    [row.asDict() for row in spark.table(SILVER_TABLE).select("domain", "institution", "reporting_year").collect()]
    if spark.catalog.tableExists(SILVER_TABLE)
    else []
)
history = query_history(row.asDict() for row in spark.table(SEARCH_LOG_TABLE).collect())
plan = plan_queries(
    LATEST_REPORTING_YEAR,
    DISCOVERY_YEARS,
    INSTITUTIONS,
    coverage(silver_rows),
    history,
    now=datetime.now(UTC),
    requery_after_days=REQUERY_AFTER_DAYS,
    dead_query_requery_days=DEAD_QUERY_REQUERY_DAYS,
)

api_key = dbutils.secrets.get(scope=cfg.serpapi_secret_scope, key=cfg.serpapi_secret_key)
with create_session() as session:
    left = searches_left(session, api_key)
budget = max(0, min(RUN_SEARCH_CAP, left - SEARCH_RESERVE))

logger.info(f"Queries due: {len(plan)}, worst case {worst_case_searches(plan)} searches")
for reason in ("missing_year", "institution", "discovery"):
    logger.info(f"  {reason}: {sum(planned.reason == reason for planned in plan)}")
logger.info(f"Searches left this month: {left}; reserve: {SEARCH_RESERVE}; this run may spend: {budget}")
for planned in plan[:20]:
    logger.info(f"  [{planned.reason}] {planned.query} (up to {planned.max_pages} pages)")

if DRY_RUN:
    dbutils.notebook.exit(f"Dry run: {len(plan)} queries due, budget {budget}; nothing searched")
if not plan:
    dbutils.notebook.exit("No queries due; every query ran recently. Nothing searched")
if budget == 0:
    dbutils.notebook.exit(f"Only {left} searches left, within the reserve of {SEARCH_RESERVE}. Nothing searched")

# COMMAND ----------
# Search for FCRs (no files are downloaded here; see silver_fcr_documents)
# The key is read from a Databricks secret, never from project_config.yml.

with create_session() as session:
    run = fetch_fcr_documents(session, api_key, plan, max_searches=budget)

# Logged before anything else, so searches already paid for are never asked again too soon
if run.log:
    spark.createDataFrame(run.log, schema=SEARCH_LOG_SCHEMA).write.mode("append").saveAsTable(SEARCH_LOG_TABLE)

asked = {entry["query"] for entry in run.log}
logger.info(f"Searches made: {len(run.log)} of {budget}, queries asked: {len(asked)} of {len(plan)}")
logger.info(f"FCR-like results: {sum(entry['fcr_results'] for entry in run.log)}")
logger.info(f"Distinct results: {len(run.documents)}")

# COMMAND ----------
# Upsert into the Bronze Table
# New results are inserted and changed ones updated; each row keeps every query that ever found it. A
# listed row is marked is_listed = false only when every one of its queries was asked again this run and
# none returned it. A new search_rank alone isn't a change, so first_seen_at survives every re-run.

if run.documents:
    spark.createDataFrame(run.documents, schema=SOURCE_SCHEMA).createOrReplaceTempView("search_results")
    bronze_merge = spark.sql(bronze_merge_sql(BRONZE_TABLE, "search_results", SOURCE_SCHEMA.fieldNames()))
    logger.info(f"Merged into {BRONZE_TABLE}:")
    bronze_merge.show()

listed = [
    row.asDict()
    for row in spark.table(BRONZE_TABLE).where(col("is_listed")).select("document_id", "search_queries").collect()
]
to_delist = delisted_ids(listed, asked, (document["document_id"] for document in run.documents))
if to_delist:
    spark.createDataFrame([(document_id,) for document_id in to_delist], "document_id string").createOrReplaceTempView(
        "delisted_results"
    )
    spark.sql(delist_sql(BRONZE_TABLE, "delisted_results"))
logger.info(f"No longer returned by any of their queries: {len(to_delist)}")

if run.error:
    raise RuntimeError(f"Search stopped early; results so far were kept: {run.error}")

# COMMAND ----------
# Verify the Data

bronze_df = spark.table(BRONZE_TABLE)

logger.info(f"Total results: {bronze_df.count()}")
bronze_df.printSchema()

logger.info("Sample records:")
bronze_df.select("document_id", "title", "domain", "search_rank", "first_seen_at").show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Results by domain:")
bronze_df.groupBy("domain").count().orderBy("count", ascending=False).show(30, truncate=False)

logger.info("Results by query:")
bronze_df.select(explode("search_queries").alias("query")).groupBy("query").count().orderBy("query").show(
    50, truncate=False
)

logger.info("Listed vs no longer returned:")
bronze_df.groupBy("is_listed").count().show()

logger.info("Searches this month by reason (this environment only):")
spark.table(SEARCH_LOG_TABLE).where(col("searched_at") >= date.today().replace(day=1).isoformat()).groupBy(
    "reason"
).agg(count("*").alias("searches"), spark_sum("fcr_results").alias("fcr_results")).show()

# COMMAND ----------
# Data Quality Checks

bronze_df.agg(
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_urls"),
    spark_sum(when(~col("pdf_url").rlike("(?i)\\.pdf$"), 1).otherwise(0)).alias("not_pdf_urls"),
    spark_sum(when(size("search_queries") > 1, 1).otherwise(0)).alias("found_by_several_queries"),
).show(truncate=False)

logger.info("Bronze FCR ingestion completed")
