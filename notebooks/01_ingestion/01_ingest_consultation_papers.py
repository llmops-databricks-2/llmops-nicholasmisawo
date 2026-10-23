# Databricks notebook source

from collections import Counter

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_date, explode, size, substring, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import curate, curate_document
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import BRONZE_SCHEMA, SILVER_METADATA_SCHEMA, SILVER_SCHEMA, SOURCE_SCHEMA
from consultation_paper_curator.sources.bma import fetch_bma_documents
from consultation_paper_curator.tables import bronze_merge_sql, ensure_table, silver_merge_sql

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.bronze_consultation_papers"
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.silver_consultation_papers"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
ensure_table(
    spark,
    BRONZE_TABLE,
    BRONZE_SCHEMA,
    layer="bronze",
    comment="Bronze: consultation documents as each source lists them, upserted on every ingest run.",
)
ensure_table(
    spark,
    SILVER_TABLE,
    SILVER_SCHEMA,
    layer="silver",
    comment="Silver: curated consultation papers (PDFs only, classified, deduplicated); "
    "processed and volume_path are set by the download job.",
)
logger.info(f"Tables {BRONZE_TABLE} and {SILVER_TABLE} ready")

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
# Upsert into the Bronze Table
# New documents are inserted, changed ones updated, and documents the site no longer lists are kept
# with is_listed = false. Unchanged rows are left alone, so first_seen_at survives every re-run.

spark.createDataFrame(publications, schema=SOURCE_SCHEMA).createOrReplaceTempView("scraped_papers")
bronze_merge = spark.sql(bronze_merge_sql(BRONZE_TABLE, "scraped_papers", SOURCE_SCHEMA.fieldNames()))

logger.info(f"Merged into {BRONZE_TABLE}:")
bronze_merge.show()

# COMMAND ----------
# Verify the Bronze Data

bronze_df = spark.table(BRONZE_TABLE)

logger.info(f"Total documents: {bronze_df.count()}")
bronze_df.printSchema()

logger.info("Sample records:")
bronze_df.select("document_id", "title", "uploaded_date", "topics", "first_seen_at").show(10, truncate=60)

# COMMAND ----------
# Bronze Statistics

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
# Bronze Data Quality Checks

bronze_df.agg(
    spark_sum(when(col("uploaded_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdf_urls"),
    spark_sum(when(col("topics").getItem(0).isNull(), 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

# COMMAND ----------
# Curate Bronze into the Silver Table
# Keeps PDFs only (spreadsheets such as BSCR models and templates can't feed the RAG pipeline),
# classifies each document, takes the publication date from the CDN filename, and keeps one
# document per title (current over archived, then newest). Curation runs over all of bronze because
# deduplication needs every document, then upserts: new papers arrive with processed and volume_path
# as None, and the MERGE never overwrites the values the 02_download job sets.

bronze_documents = [row.asDict(recursive=True) for row in bronze_df.collect()]
papers = curate(bronze_documents)

spark.createDataFrame(papers, schema=SILVER_METADATA_SCHEMA).createOrReplaceTempView("curated_papers")
silver_merge = spark.sql(silver_merge_sql(SILVER_TABLE, "curated_papers", SILVER_METADATA_SCHEMA.fieldNames()))

file_types = Counter(curate_document(document)["file_type"] for document in bronze_documents)
superseded = sum(len(paper["superseded_ids"]) for paper in papers)
logger.info(f"Bronze documents: {len(bronze_documents)} by file type: {dict(file_types)}")
logger.info(f"Curated papers: {len(papers)} ({superseded} duplicates superseded)")
logger.info(f"Merged into {SILVER_TABLE}:")
silver_merge.show()

# COMMAND ----------
# Silver Summary

silver_df = spark.table(SILVER_TABLE)

logger.info("Papers by kind:")
silver_df.groupBy("doc_kind").count().orderBy("count", ascending=False).show()

# Open/closed is computed at query time rather than stored, so it never goes stale
logger.info("Consultations open for comment:")
silver_df.filter(col("comment_deadline") >= current_date()).select(
    "document_id", "title", "comment_deadline", "submission_emails"
).orderBy("comment_deadline").show(truncate=60)

logger.info("Most recent papers:")
silver_df.select("title", "doc_kind", "published_date", "comment_deadline").orderBy(
    col("published_date").desc_nulls_last()
).show(10, truncate=60)

silver_df.agg(
    spark_sum(when(col("published_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("doc_kind") == "other", 1).otherwise(0)).alias("unclassified"),
    spark_sum(when(~col("is_listed"), 1).otherwise(0)).alias("delisted"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
    spark_sum(when(size("topics") == 0, 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

logger.info("Consultation paper ingestion completed")
