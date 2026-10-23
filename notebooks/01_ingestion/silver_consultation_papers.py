# Databricks notebook source

from collections import Counter

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_date, size, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import DOC_KINDS, curate, curate_document
from consultation_paper_curator.download import download_papers, select_for_download
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import SILVER_METADATA_SCHEMA, SILVER_SCHEMA
from consultation_paper_curator.tables import ensure_table, silver_merge_sql

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
VOLUME = cfg.volume
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.bronze_consultation_papers"
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.silver_consultation_papers"
volume_root = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

if not spark.catalog.tableExists(BRONZE_TABLE):
    raise RuntimeError(f"{BRONZE_TABLE} not found; run the bronze_consultation_papers job first")

ensure_table(
    spark,
    SILVER_TABLE,
    SILVER_SCHEMA,
    layer="silver",
    comment="Silver: curated consultation papers (PDFs only, classified, deduplicated); "
    "processed and volume_path stay None until files are downloaded.",
)
logger.info(f"Table {SILVER_TABLE} ready")

# COMMAND ----------
# Curate Bronze into the Silver Table
# Keeps PDFs only (spreadsheets such as BSCR models and templates can't feed the RAG pipeline),
# classifies each document, takes the publication date from the CDN filename, and keeps one
# document per title (current over archived, then newest). Curation runs over all of bronze because
# deduplication needs every document, then upserts: new papers arrive with processed and volume_path
# as None, and the MERGE never overwrites values set by a download.

bronze_documents = [row.asDict(recursive=True) for row in spark.table(BRONZE_TABLE).collect()]
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
# Verify the Data

silver_df = spark.table(SILVER_TABLE)

logger.info(f"Total papers: {silver_df.count()}")
silver_df.printSchema()

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

# COMMAND ----------
# Data Quality Checks

silver_df.agg(
    spark_sum(when(col("published_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("doc_kind") == "other", 1).otherwise(0)).alias("unclassified"),
    spark_sum(when(~col("is_listed"), 1).otherwise(0)).alias("delisted"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
    spark_sum(when(size("topics") == 0, 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

# COMMAND ----------
# Download Papers and Set processed / volume_path
# Off by default: until DOWNLOAD_FILES is True, processed and volume_path stay None, matching the
# course's arxiv_papers table ("Will be set in Lecture 2.2"). When True, papers with no volume_path yet
# are downloaded to /Volumes/<catalog>/<schema>/<volume>/<source>/<document_id>.pdf, and each row gets
# processed (download time as yyyyMMddHHmm) and volume_path (that file's location).

DOWNLOAD_FILES = False
MAX_DOWNLOADS = 5  # Capped while testing; None = download everything
# Kinds to leave out of the download; any of DOC_KINDS. The breakdown below shows what each choice skips.
EXCLUDED_DOC_KINDS = {"notice"}

pending_df = spark.table(SILVER_TABLE).where(col("volume_path").isNull())

logger.info(f"Doc kinds: {list(DOC_KINDS)}; excluding: {sorted(EXCLUDED_DOC_KINDS)}")
logger.info("Papers not yet downloaded, by kind:")
pending_df.groupBy("doc_kind").count().withColumn("will_download", ~col("doc_kind").isin(*EXCLUDED_DOC_KINDS)).orderBy(
    col("will_download").desc(), col("count").desc()
).show()

if not DOWNLOAD_FILES:
    dbutils.notebook.exit("Silver curation completed; downloads skipped (DOWNLOAD_FILES = False)")

spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

pending = select_for_download([row.asDict() for row in pending_df.collect()], EXCLUDED_DOC_KINDS)
logger.info(f"Papers selected for download: {len(pending)} (cap: {MAX_DOWNLOADS})")

with create_session() as session:
    stored, stats = download_papers(pending, volume_root, session, max_downloads=MAX_DOWNLOADS)

logger.info(f"Papers downloaded: {stats.downloaded}, already present: {stats.skipped}, failed: {stats.failed}")

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

logger.info(f"Set processed and volume_path on {len(stored)} rows in {SILVER_TABLE}")

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

logger.info("Silver consultation paper curation and download completed")
