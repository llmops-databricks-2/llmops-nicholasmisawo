# Databricks notebook source

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import DOC_KINDS
from consultation_paper_curator.download import download_papers, select_for_download
from consultation_paper_curator.http_client import create_session

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
VOLUME = cfg.volume
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.silver_consultation_papers"
volume_root = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

# COMMAND ----------
# Download Curated Papers to the Unity Catalog Volume
# Only papers whose volume_path is still None are fetched; the 01_ingest job writes the silver table.

MAX_DOWNLOADS = 5  # Capped while testing; None = download everything
# Kinds to leave out of the download; any of DOC_KINDS. The breakdown below shows what each choice skips.
EXCLUDED_DOC_KINDS = {"notice"}

logger.info(f"Doc kinds: {list(DOC_KINDS)}; excluding: {sorted(EXCLUDED_DOC_KINDS)}")

spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

pending_df = spark.table(SILVER_TABLE).where(col("volume_path").isNull())

logger.info("Papers not yet downloaded, by kind:")
pending_df.groupBy("doc_kind").count().withColumn("will_download", ~col("doc_kind").isin(*EXCLUDED_DOC_KINDS)).orderBy(
    col("will_download").desc(), col("count").desc()
).show()

pending = select_for_download([row.asDict() for row in pending_df.collect()], EXCLUDED_DOC_KINDS)
logger.info(f"Papers selected for download: {len(pending)} (cap: {MAX_DOWNLOADS})")

with create_session() as session:
    stored, stats = download_papers(pending, volume_root, session, max_downloads=MAX_DOWNLOADS)

logger.info(f"Papers downloaded: {stats.downloaded}, already present: {stats.skipped}, failed: {stats.failed}")

# COMMAND ----------
# Record processed and volume_path in the Silver Table

if stored:
    updates = spark.createDataFrame(
        [(document_id, file.processed, file.volume_path) for document_id, file in stored.items()],
        "document_id string, processed long, volume_path string",
    )
    updates.createOrReplaceTempView("downloaded_papers")
    spark.sql(f"""
        MERGE INTO {SILVER_TABLE} AS papers
        USING downloaded_papers AS downloaded
        ON papers.document_id = downloaded.document_id
        WHEN MATCHED THEN UPDATE SET
            papers.processed = downloaded.processed,
            papers.volume_path = downloaded.volume_path
    """)

logger.info(f"Updated {len(stored)} rows in {SILVER_TABLE}")

# COMMAND ----------
# Verify the Downloads

papers_df = spark.table(SILVER_TABLE)

logger.info("Download status by kind:")
papers_df.groupBy("doc_kind").agg(
    spark_sum(when(col("volume_path").isNotNull(), 1).otherwise(0)).alias("downloaded"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
).withColumn("excluded", col("doc_kind").isin(*EXCLUDED_DOC_KINDS)).orderBy("excluded", "doc_kind").show()

logger.info("Most recently downloaded:")
papers_df.where(col("volume_path").isNotNull()).select("document_id", "title", "processed", "volume_path").orderBy(
    col("processed").desc()
).show(10, truncate=60)

logger.info("Consultation paper download completed")
