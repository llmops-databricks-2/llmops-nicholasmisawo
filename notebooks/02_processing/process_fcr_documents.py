# Databricks notebook source
# MAGIC %md
# MAGIC # Process: parse FCR PDFs and split them into chunks
# MAGIC
# MAGIC 1. Remove parsed rows and chunks of FCRs silver no longer has
# MAGIC 2. Parse newly downloaded PDFs with `ai_parse_document` into `silver_parsed_fcr_documents`
# MAGIC 3. Turn each text and table element into a cleaned chunk with its FCR's metadata, in `gold_fcr_chunks`
# MAGIC
# MAGIC Chunks are not embedded yet. Why `ai_parse_document`:
# MAGIC
# MAGIC | Tool | Fit for Bermuda FCRs |
# MAGIC |------|---------------------|
# MAGIC | **AI Parse Documents** | Best: handles every insurer's layout and keeps the solvency and capital tables; runs in the workspace |
# MAGIC | PyPDF2 | Loses tables; poor with complex layouts |
# MAGIC | pdfplumber | Good tables, but needs tuning per insurer template |
# MAGIC | Unstructured.io | Sends documents to an outside service |

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count, length, percentile_approx
from pyspark.sql.functions import max as spark_max
from pyspark.sql.functions import min as spark_min

from fcr_curator.config import load_config
from fcr_curator.data_processor import DataProcessor

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
processor = DataProcessor(spark=spark, config=cfg)

# COMMAND ----------
# 1-3. Parse and chunk (each FCR is handled once; re-runs only pick up new downloads)

processor.process_and_save()

# COMMAND ----------
# Summary

parsed_df = spark.table(processor.parsed_table)
chunks_df = spark.table(processor.chunks_table)
logger.info(f"Parsed FCRs: {parsed_df.count()}; chunks: {chunks_df.count()}")

# Chunk sizes in characters: the starting point for reviewing the chunking strategy
chunks_df.select(
    spark_min(length("text")).alias("min_chars"),
    percentile_approx(length("text"), 0.5).alias("median_chars"),
    spark_max(length("text")).alias("max_chars"),
).show()

chunks_df.groupBy("institution", "reporting_year").agg(count("*").alias("chunks")).orderBy(col("chunks").desc()).show(
    10, truncate=60
)

# Parsed FCRs with no chunks are retried every run; list them so they can be looked at
parsed_df.join(chunks_df.select("document_id").distinct(), "document_id", "left_anti").select("volume_path").show(
    truncate=False
)
