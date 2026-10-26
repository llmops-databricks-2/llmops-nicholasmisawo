# Databricks notebook source
# MAGIC %md
# MAGIC # Process FCRs: PDF Parsing with AI Parse Documents
# MAGIC
# MAGIC Parses the Financial Condition Reports the silver job downloaded to the volume, splits them into clean
# MAGIC text and table chunks, enriches each chunk with its document's metadata, and embeds it.
# MAGIC
# MAGIC | Tool | Pros | Cons | Fit for Bermuda FCRs |
# MAGIC |------|------|------|---------------------|
# MAGIC | **AI Parse Documents** | AI-powered; handles complex layouts; preserves structure and tables; runs in Databricks SQL | Databricks-specific; cost per page | Best: every insurer uses its own layout, and solvency and capital tables carry the figures |
# MAGIC | **PyPDF2** | Simple; free; pure Python | Poor with complex layouts; no table extraction | Loses the capital and solvency tables |
# MAGIC | **pdfplumber** | Good table extraction; layout analysis | Slower; per-document tuning | Needs tuning per insurer template |
# MAGIC | **Apache Tika** | Multi-format; metadata extraction | Java dependency; heavy | Overkill: silver keeps PDFs only |
# MAGIC | **Unstructured.io** | ML-powered; good chunking | External service; API costs | Sends documents outside the workspace |
# MAGIC
# MAGIC **AI Parse Documents** is used: it keeps parsing inside the workspace and governed by Unity Catalog.

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count, when
from pyspark.sql.functions import sum as spark_sum

from fcr_curator.config import load_config
from fcr_curator.data_processor import DataProcessor

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

processor = DataProcessor(spark=spark, config=cfg)
logger.info(f"Catalog: {cfg.catalog}, Schema: {cfg.schema}, Volume: {cfg.volume}")

# COMMAND ----------
# Parse PDFs, Create Chunks and Embed Them
# 1. Removes parsed rows and chunks of documents silver no longer holds (filtered out or superseded)
# 2. Parses downloaded documents not yet parsed with ai_parse_document into silver_parsed_fcr_documents
# 3. Extracts and cleans text and table chunks, joins the document metadata, embeds each chunk with the
#    configured embedding endpoint and appends them to gold_fcr_chunks (Change Data Feed on)
# Each document is parsed and embedded once, so re-runs only process documents downloaded since.

processor.process_and_save()

# COMMAND ----------
# Verify the Data

parsed_df = spark.table(processor.parsed_table)
chunks_df = spark.table(processor.chunks_table)

logger.info(f"Parsed documents: {parsed_df.count()}, chunks: {chunks_df.count()}")
chunks_df.printSchema()

logger.info("Chunks per document:")
chunks_df.groupBy("document_id", "institution", "reporting_year").agg(count("*").alias("chunks")).orderBy(
    col("chunks").desc()
).show(10, truncate=60)

logger.info("Sample chunks:")
chunks_df.select("id", "page_id", "title", "text").show(5, truncate=80)

# COMMAND ----------
# Data Quality Checks

parsed_df.agg(
    spark_sum(when(col("parse_errors").isNotNull() & (col("parse_errors") != "[]"), 1).otherwise(0)).alias(
        "documents_with_page_errors"
    ),
).show(truncate=False)

# Parsed documents that yielded no chunks are retried by every run, so they show up here until fixed
parsed_df.join(chunks_df.select("document_id").distinct(), "document_id", "left_anti").select(
    "document_id", "volume_path"
).show(truncate=False)

chunks_df.agg(
    spark_sum(when(col("embedding").isNull(), 1).otherwise(0)).alias("missing_embeddings"),
    spark_sum(when(col("page_id").isNull(), 1).otherwise(0)).alias("missing_pages"),
).show(truncate=False)

logger.info("FCR processing completed")
