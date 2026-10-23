# Databricks notebook source

from collections import Counter

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_date, size, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import curate, curate_document
from consultation_paper_curator.download import download_papers, with_storage
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import CURATED_SCHEMA

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
VOLUME = cfg.volume
RAW_TABLE = f"{CATALOG}.{SCHEMA}.bma_consultation_papers"
CURATED_TABLE = f"{CATALOG}.{SCHEMA}.consultation_papers"
volume_root = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

# COMMAND ----------
# Curate the Raw Metadata
# Keeps PDFs only (spreadsheets such as BSCR models and templates can't feed the RAG pipeline),
# classifies each document, takes the publication date from the CDN filename, and keeps one
# document per title (current over archived, then newest).

raw_documents = [row.asDict(recursive=True) for row in spark.table(RAW_TABLE).collect()]
papers = curate(raw_documents)

file_types = Counter(curate_document(raw)["file_type"] for raw in raw_documents)
superseded = sum(len(paper["superseded_ids"]) for paper in papers)
logger.info(f"Raw documents: {len(raw_documents)} by file type: {dict(file_types)}")
logger.info(f"Curated papers: {len(papers)} ({superseded} duplicates superseded)")
logger.info(f"By kind: {dict(Counter(paper['doc_kind'] for paper in papers))}")

# COMMAND ----------
# Download Curated Papers to the Unity Catalog Volume

MAX_DOWNLOADS = 5  # Capped while testing; None = download everything

spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

with create_session() as session:
    stored, stats = download_papers(papers, volume_root, session, max_downloads=MAX_DOWNLOADS)

logger.info(f"Papers downloaded: {stats.downloaded}, already present: {stats.skipped}, failed: {stats.failed}")

rows = with_storage(papers, stored)

# COMMAND ----------
# Create Curated Delta Table in Unity Catalog

df = spark.createDataFrame(rows, schema=CURATED_SCHEMA)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(CURATED_TABLE)

logger.info(f"Created Delta table: {CURATED_TABLE}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Data

papers_df = spark.table(CURATED_TABLE)

logger.info("Papers by kind:")
papers_df.groupBy("doc_kind").count().orderBy("count", ascending=False).show()

# Open/closed is computed at query time rather than stored, so it never goes stale
logger.info("Consultations open for comment:")
papers_df.filter(col("comment_deadline") >= current_date()).select(
    "document_id", "title", "comment_deadline", "submission_emails"
).orderBy("comment_deadline").show(truncate=60)

logger.info("Most recent papers:")
papers_df.select("title", "doc_kind", "published_date", "comment_deadline").orderBy(
    col("published_date").desc_nulls_last()
).show(10, truncate=60)

# COMMAND ----------
# Data Quality Checks

quality_df = papers_df.agg(
    spark_sum(when(col("published_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("doc_kind") == "other", 1).otherwise(0)).alias("unclassified"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("missing_downloads"),
    spark_sum(when(size("topics") == 0, 1).otherwise(0)).alias("untagged_topics"),
)

quality_df.show(truncate=False)

logger.info("Consultation paper curation completed")
