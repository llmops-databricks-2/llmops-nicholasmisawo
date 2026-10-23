# Databricks notebook source

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, explode, substring, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import BRONZE_SCHEMA, SOURCE_SCHEMA
from consultation_paper_curator.sources.bma import fetch_bma_documents
from consultation_paper_curator.tables import bronze_merge_sql, ensure_table

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.bronze_consultation_papers"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
ensure_table(
    spark,
    BRONZE_TABLE,
    BRONZE_SCHEMA,
    layer="bronze",
    comment="Bronze: consultation documents as each source lists them, upserted on every ingest run.",
)
logger.info(f"Table {BRONZE_TABLE} ready")

# COMMAND ----------
# Fetch BMA Consultation Paper Metadata (no files are downloaded here; see silver_consultation_papers)

MAX_RESULTS = None  # None = keep everything; set an int to cap the most recent documents

logger.info("Fetching BMA consultation papers...")
with create_session() as session:
    publications = fetch_bma_documents(session, max_results=MAX_RESULTS)

if not publications:
    raise ValueError("No BMA consultation papers found")

logger.info(f"Fetched {len(publications)} documents")
logger.info(f"Sample: {publications[0]['title']}")
logger.info(f"Uploaded: {publications[0]['uploaded_date']}")
logger.info(f"Categories: {publications[0]['categories']}")
logger.info(f"Topics: {publications[0]['topics']}")
logger.info(f"PDF URL: {publications[0]['pdf_url']}")

# COMMAND ----------
# Upsert into the Bronze Table
# New documents are inserted, changed ones updated, and documents the site no longer lists are kept
# with is_listed = false. Unchanged rows are left alone, so first_seen_at survives every re-run.

spark.createDataFrame(publications, schema=SOURCE_SCHEMA).createOrReplaceTempView("scraped_papers")
bronze_merge = spark.sql(bronze_merge_sql(BRONZE_TABLE, "scraped_papers", SOURCE_SCHEMA.fieldNames()))

logger.info(f"Merged into {BRONZE_TABLE}:")
bronze_merge.show()

# COMMAND ----------
# Verify the Data

bronze_df = spark.table(BRONZE_TABLE)

logger.info(f"Total documents: {bronze_df.count()}")
bronze_df.printSchema()

logger.info("Sample records:")
bronze_df.select("document_id", "title", "uploaded_date", "topics", "first_seen_at").show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Documents by category:")
bronze_df.select(explode("categories").alias("category")).groupBy("category").count().orderBy(
    "count", ascending=False
).show(truncate=False)

logger.info("Documents by topic:")
bronze_df.select(explode("topics").alias("topic")).groupBy("topic").count().orderBy("count", ascending=False).show(
    50, truncate=False
)

logger.info("Listed vs delisted, current vs archived:")
bronze_df.groupBy("is_listed", "is_archived").count().orderBy("is_listed", "is_archived").show()

logger.info("Documents by year:")
bronze_df.groupBy(substring("uploaded_date", 1, 4).alias("year")).count().orderBy("year", ascending=False).show(30)

logger.info("Most recent documents:")
bronze_df.select("title", "uploaded_date", "comment_deadline").orderBy(col("uploaded_date").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Data Quality Checks

bronze_df.agg(
    spark_sum(when(col("uploaded_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdf_urls"),
    spark_sum(when(col("topics").getItem(0).isNull(), 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

logger.info("Bronze consultation paper ingestion completed")
