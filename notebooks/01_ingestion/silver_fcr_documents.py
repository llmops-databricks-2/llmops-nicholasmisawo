# Databricks notebook source

from collections import Counter  # noqa: I001

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, when
from pyspark.sql.functions import sum as spark_sum

from fcr_curator.config import load_config
from fcr_curator.curation import (
    DOC_KINDS,
    curate,
    curate_document,
    keep_doc_kinds,
    keep_reported_since,
)
from fcr_curator.download import download_documents
from fcr_curator.http_client import create_session
from fcr_curator.schemas import SILVER_METADATA_SCHEMA, SILVER_SCHEMA
from fcr_curator.tables import ensure_table, silver_merge_sql

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
VOLUME = cfg.volume
BRONZE_TABLE = f"{CATALOG}.{SCHEMA}.bronze_fcr_documents"
SILVER_TABLE = f"{CATALOG}.{SCHEMA}.silver_fcr_documents"
volume_root = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
# Kinds kept in silver, and so the only ones downloaded; any of DOC_KINDS. SFCRs are the EU/UK
# Solvency II report, not Bermuda's FCR, so they stay out unless listed here.
INCLUDED_DOC_KINDS = {"fcr"}
# Earliest reporting year kept in silver (and so downloaded); None keeps every year. Documents with no
# reporting year are left out while a year is set, and older ones already in silver are removed by the
# next merge.
MIN_REPORTING_YEAR = 2020

if not spark.catalog.tableExists(BRONZE_TABLE):
    raise RuntimeError(f"{BRONZE_TABLE} not found; run the bronze_fcr_documents task of fcr_pipeline first")

ensure_table(
    spark,
    SILVER_TABLE,
    SILVER_SCHEMA,
    layer="silver",
    comment="Silver: curated FCRs (PDFs only, classified, one per institution and year, "
    f"kinds {sorted(INCLUDED_DOC_KINDS)} only"
    + (f", reporting on {MIN_REPORTING_YEAR} or later" if MIN_REPORTING_YEAR else "")
    + "); "
    "processed and volume_path stay None until files are downloaded.",
)
logger.info(f"Table {SILVER_TABLE} ready")

# COMMAND ----------
# Curate Bronze into the Silver Table
# Keeps PDFs only, classifies each result (FCR, SFCR or other) from its title, filename and snippet,
# extracts the institution and reporting year, and keeps one document per institution and year on each
# domain (still returned by search first, then best ranked), then keeps only INCLUDED_DOC_KINDS reporting
# on MIN_REPORTING_YEAR or later so silver holds only documents that will be downloaded.
# Curation runs over all of bronze because deduplication needs every result, then upserts: new
# documents arrive with processed and volume_path as None, and the MERGE never overwrites values set
# by a download.

bronze_documents = [row.asDict(recursive=True) for row in spark.table(BRONZE_TABLE).collect()]
curated = curate(bronze_documents)
of_kind = keep_doc_kinds(curated, INCLUDED_DOC_KINDS)
documents = keep_reported_since(of_kind, MIN_REPORTING_YEAR)

spark.createDataFrame(documents, schema=SILVER_METADATA_SCHEMA).createOrReplaceTempView("curated_documents")
silver_merge = spark.sql(silver_merge_sql(SILVER_TABLE, "curated_documents", SILVER_METADATA_SCHEMA.fieldNames()))

file_types = Counter(curate_document(document)["file_type"] for document in bronze_documents)
superseded = sum(len(document["superseded_ids"]) for document in curated)
left_out_kinds = Counter(document["doc_kind"] for document in curated if document["doc_kind"] not in INCLUDED_DOC_KINDS)
logger.info(f"Bronze results: {len(bronze_documents)} by file type: {dict(file_types)}")
logger.info(f"Curated documents: {len(curated)} ({superseded} duplicates superseded)")
logger.info(f"Doc kinds: {list(DOC_KINDS)}; keeping: {sorted(INCLUDED_DOC_KINDS)}")
logger.info(f"Documents of the kept kinds: {len(of_kind)} (left out by kind: {dict(left_out_kinds)})")
logger.info(
    f"Documents kept for silver: {len(documents)} (min reporting year: {MIN_REPORTING_YEAR}; "
    f"{len(of_kind) - len(documents)} older or undated left out)"
)
logger.info(f"Merged into {SILVER_TABLE}:")
silver_merge.show()

# COMMAND ----------
# Verify the Data

silver_df = spark.table(SILVER_TABLE)

logger.info(f"Total FCRs: {silver_df.count()}")
silver_df.printSchema()

logger.info("FCRs by reporting year:")
silver_df.groupBy("reporting_year").count().orderBy(col("reporting_year").desc()).show()

logger.info("FCRs by institution:")
silver_df.groupBy("institution", "domain").count().orderBy("institution").show(50, truncate=60)

# COMMAND ----------
# Data Quality Checks

silver_df.agg(
    spark_sum(when(col("institution").isNull(), 1).otherwise(0)).alias("missing_institutions"),
    spark_sum(when(~col("is_listed"), 1).otherwise(0)).alias("no_longer_returned"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
).show(truncate=False)

# COMMAND ----------
# Download FCRs and Set processed / volume_path
# Documents with no volume_path yet are downloaded to
# /Volumes/<catalog>/<schema>/<volume>/<source>/<document_id>.pdf, and each row gets processed (download
# time as yyyyMMddHHmm) and volume_path (that file's location), as in the course's arxiv_papers table
# (Lecture 2.2). Set DOWNLOAD_FILES to False to stop after curation and leave both as None.

DOWNLOAD_FILES = True
MAX_DOWNLOADS = None  # None = download everything; set a number to cap new attempts per run

pending_df = spark.table(SILVER_TABLE).where(col("volume_path").isNull())
logger.info(f"FCRs not yet downloaded: {pending_df.count()}")

if not DOWNLOAD_FILES:
    dbutils.notebook.exit("Silver curation completed; downloads skipped (DOWNLOAD_FILES = False)")

spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")

pending = [row.asDict() for row in pending_df.collect()]
logger.info(f"FCRs selected for download: {len(pending)} (cap: {MAX_DOWNLOADS})")

with create_session() as session:
    stored, stats = download_documents(pending, volume_root, session, max_downloads=MAX_DOWNLOADS)

logger.info(
    f"FCRs downloaded: {stats.downloaded}, already present: {stats.skipped}, failed: {stats.failed}, "
    f"not a PDF (removed): {stats.rejected}"
)

if stored:
    updates = spark.createDataFrame(
        [(document_id, file.processed, file.volume_path) for document_id, file in stored.items()],
        "document_id string, processed long, volume_path string",
    )
    updates.createOrReplaceTempView("downloaded_documents")
    spark.sql(f"""
        MERGE INTO {SILVER_TABLE} AS documents
        USING downloaded_documents AS downloaded
        ON documents.document_id = downloaded.document_id
        WHEN MATCHED THEN UPDATE SET
            documents.processed = downloaded.processed,
            documents.volume_path = downloaded.volume_path
    """)

logger.info(f"Set processed and volume_path on {len(stored)} rows in {SILVER_TABLE}")

# COMMAND ----------
# Verify the Downloads

documents_df = spark.table(SILVER_TABLE)

logger.info("Download status by reporting year:")
documents_df.groupBy("reporting_year").agg(
    spark_sum(when(col("volume_path").isNotNull(), 1).otherwise(0)).alias("downloaded"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("not_downloaded"),
).orderBy(col("reporting_year").desc()).show()

# Insurer sites block or move files more often than a regulator's CDN; failures are retried next run
logger.info("Not downloaded (check these URLs by hand if they keep failing):")
documents_df.where(col("volume_path").isNull()).select("institution", "reporting_year", "file_url").show(
    20, truncate=80
)

logger.info("Silver FCR curation and download completed")
