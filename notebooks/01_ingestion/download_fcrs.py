# Databricks notebook source
# MAGIC %md
# MAGIC # Download the FCRs in the register into the volume
# MAGIC
# MAGIC dbt curates the search results into `fcr_register` first (`dbt/models/intermediate/int_fcr_register.py`).
# MAGIC This downloads each FCR whose PDF isn't in the volume yet, to the path the register gives. dbt reads
# MAGIC the volume to see what's downloaded, then parses it. Every attempt is appended to
# MAGIC `raw_fcr_download_attempts`, so `fcr_register` shows FCRs that keep failing.
# MAGIC
# MAGIC Uses no SerpAPI searches.

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col

from fcr_curator.config import load_config
from fcr_curator.download import download_documents
from fcr_curator.http_client import create_session
from fcr_curator.landing import append_facts
from fcr_curator.tables import DOWNLOAD_ATTEMPTS_TABLE, REGISTER_TABLE

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))

MAX_DOWNLOADS = None  # None downloads everything pending; a number caps attempts per run

REGISTER = f"{cfg.full_schema_name}.{REGISTER_TABLE}"
VOLUME_ROOT = f"/Volumes/{cfg.catalog}/{cfg.schema}/{cfg.volume}"

if not spark.catalog.tableExists(REGISTER):
    raise RuntimeError(f"{REGISTER} not found; run the dbt_register task first")

# COMMAND ----------
# Download what's missing (files already in the volume are skipped), then land every attempt

pending = [row.asDict() for row in spark.table(REGISTER).where(col("downloaded_at").isNull()).collect()]
with create_session() as session:
    attempts, stats = download_documents(pending, VOLUME_ROOT, session, max_downloads=MAX_DOWNLOADS)
logger.info(f"Pending: {len(pending)}; {stats}")
append_facts(spark, cfg.full_schema_name, {DOWNLOAD_ATTEMPTS_TABLE: attempts})

# COMMAND ----------
# Summary: failed downloads are retried next run; check a URL by hand if it keeps failing

register_df = spark.table(REGISTER)
register_df.groupBy("reporting_year").count().orderBy(col("reporting_year").desc()).show()
register_df.where(col("downloaded_at").isNull()).select(
    "institution", "reporting_year", "download_attempts", "last_download_outcome", "file_url"
).show(truncate=80)
