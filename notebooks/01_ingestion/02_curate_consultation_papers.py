# Databricks notebook source

import os
import time
from collections import Counter
from datetime import UTC, datetime

import requests
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, current_date, size, when
from pyspark.sql.functions import sum as spark_sum
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from consultation_paper_curator.config import load_config
from consultation_paper_curator.curation import curate, curate_document

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


def create_session():
    """Create HTTP session with retries."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "ConsultationPaperCurator/1.0"})
    return session


def download_document(session, url, path):
    """Stream a document to the volume; return True on success."""
    try:
        with session.get(url, timeout=120, stream=True) as response:
            response.raise_for_status()
            with open(path, "wb") as f:
                f.writelines(response.iter_content(chunk_size=1 << 20))
        return True
    except (requests.RequestException, OSError) as exc:
        logger.warning(f"Failed to download {url}: {exc}")
        if os.path.exists(path):
            os.remove(path)
        return False


downloaded = skipped = failed = 0

with create_session() as session:
    for paper in papers:
        paper["volume_path"] = paper["downloaded_at"] = None
        source_dir = f"{volume_root}/{paper['source']}"
        os.makedirs(source_dir, exist_ok=True)
        path = f"{source_dir}/{paper['document_id']}.{paper['file_type']}"

        if os.path.exists(path):
            skipped += 1
        elif MAX_DOWNLOADS is not None and downloaded + failed >= MAX_DOWNLOADS:
            continue
        elif download_document(session, paper["file_url"], path):
            downloaded += 1
            time.sleep(1)
        else:
            failed += 1
            continue

        paper["volume_path"] = path
        paper["downloaded_at"] = datetime.fromtimestamp(os.path.getmtime(path), UTC)

logger.info(f"Papers downloaded: {downloaded}, already present: {skipped}, failed: {failed}")

# COMMAND ----------
# Create Curated Delta Table in Unity Catalog

schema = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("source", StringType(), True),
        StructField("institution", StringType(), True),
        StructField("title", StringType(), False),
        StructField("doc_kind", StringType(), False),
        StructField("file_type", StringType(), True),
        StructField("file_url", StringType(), True),
        StructField("published_date", DateType(), True),
        StructField("listed_date", DateType(), True),
        StructField("file_uploaded_at", TimestampType(), True),
        StructField("comment_deadline", DateType(), True),
        StructField("submission_note", StringType(), True),
        StructField("submission_emails", ArrayType(StringType()), True),
        StructField("topics", ArrayType(StringType()), True),
        StructField("is_archived", BooleanType(), True),
        StructField("related_documents", ArrayType(StringType()), True),
        StructField("superseded_ids", ArrayType(StringType()), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("downloaded_at", TimestampType(), True),
        StructField("volume_path", StringType(), True),
    ]
)

df = spark.createDataFrame(papers, schema=schema)

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
