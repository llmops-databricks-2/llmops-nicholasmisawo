# Databricks notebook source

from collections import Counter

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_date, explode, size, substring, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import curate, curate_document
from consultation_paper_curator.download import StoredFile, with_storage
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import CURATED_SCHEMA, RAW_SCHEMA
from consultation_paper_curator.sources.bma import fetch_bma_documents

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
TABLE_NAME = "bma_consultation_papers"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"
CURATED_TABLE = f"{CATALOG}.{SCHEMA}.consultation_papers"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
logger.info(f"Schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------
# Fetch BMA Consultation Paper Metadata (no files are downloaded here; see 02_download)

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
# Create Raw Delta Table in Unity Catalog

df = spark.createDataFrame(publications, schema=RAW_SCHEMA)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_path)

logger.info(f"Created Delta table: {table_path}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Raw Data

papers_df = spark.table(table_path)

logger.info(f"Table: {table_path}")
logger.info(f"Total documents: {papers_df.count()}")
papers_df.printSchema()

logger.info("Sample records:")
papers_df.select("document_id", "title", "uploaded_date", "topics").show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Documents by category:")
papers_df.select(explode("categories").alias("category")).groupBy("category").count().orderBy(
    "count", ascending=False
).show(truncate=False)

logger.info("Documents by topic:")
papers_df.select(explode("topics").alias("topic")).groupBy("topic").count().orderBy("count", ascending=False).show(
    50, truncate=False
)

logger.info("Current vs archived:")
papers_df.groupBy("is_archived").count().show()

logger.info("Documents by year:")
papers_df.groupBy(substring("uploaded_date", 1, 4).alias("year")).count().orderBy("year", ascending=False).show(30)

logger.info("Most recent documents:")
papers_df.select("title", "uploaded_date", "comment_deadline").orderBy(col("uploaded_date").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Raw Data Quality Checks

quality_df = papers_df.agg(
    spark_sum(when(col("uploaded_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdf_urls"),
    spark_sum(when(col("topics").getItem(0).isNull(), 1).otherwise(0)).alias("untagged_topics"),
)

quality_df.show(truncate=False)

# COMMAND ----------
# Curate into the consultation_papers Delta Table
# Keeps PDFs only (spreadsheets such as BSCR models and templates can't feed the RAG pipeline),
# classifies each document, takes the publication date from the CDN filename, and keeps one
# document per title (current over archived, then newest). processed and volume_path start as None
# and are set by the 02_download job; files already downloaded keep their values across re-runs.

raw_documents = [row.asDict(recursive=True) for row in papers_df.collect()]
papers = curate(raw_documents)

downloaded = {}
if spark.catalog.tableExists(CURATED_TABLE):
    downloaded = {
        row.document_id: StoredFile(row.volume_path, row.processed)
        for row in spark.table(CURATED_TABLE).where(col("volume_path").isNotNull()).collect()
    }

curated_df = spark.createDataFrame(with_storage(papers, downloaded), schema=CURATED_SCHEMA)
curated_df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(CURATED_TABLE)

file_types = Counter(curate_document(raw)["file_type"] for raw in raw_documents)
superseded = sum(len(paper["superseded_ids"]) for paper in papers)
logger.info(f"Raw documents: {len(raw_documents)} by file type: {dict(file_types)}")
logger.info(f"Curated papers: {len(papers)} ({superseded} duplicates superseded)")
logger.info(f"Created Delta table: {CURATED_TABLE} ({len(downloaded)} downloads carried over)")

# COMMAND ----------
# Curated Summary

curated_df = spark.table(CURATED_TABLE)

logger.info("Papers by kind:")
curated_df.groupBy("doc_kind").count().orderBy("count", ascending=False).show()

# Open/closed is computed at query time rather than stored, so it never goes stale
logger.info("Consultations open for comment:")
curated_df.filter(col("comment_deadline") >= current_date()).select(
    "document_id", "title", "comment_deadline", "submission_emails"
).orderBy("comment_deadline").show(truncate=60)

logger.info("Most recent papers:")
curated_df.select("title", "doc_kind", "published_date", "comment_deadline").orderBy(
    col("published_date").desc_nulls_last()
).show(10, truncate=60)

curated_df.agg(
    spark_sum(when(col("published_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("doc_kind") == "other", 1).otherwise(0)).alias("unclassified"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
    spark_sum(when(size("topics") == 0, 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

logger.info("Consultation paper ingestion completed")
