# Databricks notebook source
# MAGIC %md
# MAGIC # Silver: curate FCRs and download them
# MAGIC
# MAGIC 1. Curate bronze (`curation.py`): PDFs only, not from `bma.bm`, one per institution and year, each named
# MAGIC    after its entity on the reference list (`registered_name`), matched by domain, then by name
# MAGIC 2. Keep only the wanted kinds and years, and merge them into `silver_fcr_documents`
# MAGIC 3. Download FCRs not yet in the volume and record where each file went
# MAGIC
# MAGIC Uses no SerpAPI searches.

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

from fcr_curator.config import find_project_file, load_config
from fcr_curator.curation import curate, keep_doc_kinds, keep_reported_since
from fcr_curator.download import download_documents
from fcr_curator.http_client import create_session
from fcr_curator.reference import REFERENCE_FILE, entity_resolver, load_reference_entities
from fcr_curator.schemas import SILVER_METADATA_SCHEMA, SILVER_SCHEMA
from fcr_curator.tables import (
    add_missing_columns,
    align_column_order,
    ensure_table,
    record_downloads_sql,
    silver_merge_sql,
)

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))

INCLUDED_DOC_KINDS = {"fcr"}  # "other" (annual reports, press releases...) stays out
MIN_REPORTING_YEAR = 2020  # None keeps every year
DOWNLOAD_FILES = True
MAX_DOWNLOADS = None  # None downloads everything pending; a number caps attempts per run

BRONZE_TABLE = f"{cfg.full_schema_name}.bronze_fcr_documents"
SILVER_TABLE = f"{cfg.full_schema_name}.silver_fcr_documents"
VOLUME_ROOT = f"/Volumes/{cfg.catalog}/{cfg.schema}/{cfg.volume}"

if not spark.catalog.tableExists(BRONZE_TABLE):
    raise RuntimeError(f"{BRONZE_TABLE} not found; run the bronze_fcr_documents task first")
ensure_table(spark, SILVER_TABLE, SILVER_SCHEMA, "silver", "Silver: curated FCRs, one per institution and year.")
add_missing_columns(spark, SILVER_TABLE, SILVER_SCHEMA)  # Columns added to the schema after the table was made
align_column_order(spark, SILVER_TABLE, SILVER_SCHEMA)  # ...which land at the end until moved into place
resolve_entity = entity_resolver(load_reference_entities(find_project_file(REFERENCE_FILE)))

# COMMAND ----------
# 1-2. Curate all of bronze (de-duplication needs every row), filter, and merge into silver

bronze_rows = [row.asDict(recursive=True) for row in spark.table(BRONZE_TABLE).collect()]
curated = curate(bronze_rows, resolve_entity=resolve_entity)
documents = keep_reported_since(keep_doc_kinds(curated, INCLUDED_DOC_KINDS), MIN_REPORTING_YEAR)
logger.info(f"Bronze rows: {len(bronze_rows)} -> curated: {len(curated)} -> kept for silver: {len(documents)}")
named = sum(document["registered_name"] is not None for document in documents)
logger.info(f"Matched to the reference list: {named} of {len(documents)}")

spark.createDataFrame(documents, schema=SILVER_METADATA_SCHEMA).createOrReplaceTempView("curated")
spark.sql(silver_merge_sql(SILVER_TABLE, "curated", SILVER_METADATA_SCHEMA.fieldNames())).show()

if not DOWNLOAD_FILES:
    dbutils.notebook.exit("Curated; downloads skipped (DOWNLOAD_FILES = False)")

# COMMAND ----------
# 3. Download what's missing, then set processed and volume_path on those rows

spark.sql(f"CREATE VOLUME IF NOT EXISTS {cfg.full_schema_name}.{cfg.volume}")
pending = [row.asDict() for row in spark.table(SILVER_TABLE).where(col("volume_path").isNull()).collect()]

with create_session() as session:
    stored, stats = download_documents(pending, VOLUME_ROOT, session, max_downloads=MAX_DOWNLOADS)
logger.info(f"Pending: {len(pending)}; {stats}")

if stored:
    spark.createDataFrame(
        [(document_id, file.processed, file.volume_path) for document_id, file in stored.items()],
        "document_id string, processed long, volume_path string",
    ).createOrReplaceTempView("downloads")
    spark.sql(record_downloads_sql(SILVER_TABLE, "downloads"))

# COMMAND ----------
# Summary: failed downloads are retried next run; check a URL by hand if it keeps failing

silver_df = spark.table(SILVER_TABLE)
silver_df.groupBy("reporting_year").count().orderBy(col("reporting_year").desc()).show()
silver_df.groupBy("registered_name").count().orderBy(col("count").desc()).show(30, truncate=False)
silver_df.where(col("volume_path").isNull()).select("institution", "reporting_year", "file_url").show(truncate=80)
