# Databricks notebook source
# MAGIC %md
# MAGIC # Process Consultation Papers: PDF Parsing with AI Parse Documents
# MAGIC
# MAGIC Parses the BMA consultation papers the silver job downloaded to the volume, splits them into clean
# MAGIC text and table chunks, enriches each chunk with its paper's metadata, and embeds it.
# MAGIC
# MAGIC | Tool | Pros | Cons | Fit for BMA consultation papers |
# MAGIC |------|------|------|---------------------------------|
# MAGIC | **AI Parse Documents** | AI-powered; handles complex layouts; preserves structure and tables; runs in Databricks SQL | Databricks-specific; cost per page | Best: multi-column layouts, fee and capital tables, scanned annexes |
# MAGIC | **PyPDF2** | Simple; free; pure Python | Poor with complex layouts; no table extraction | Loses the tables proposals hinge on |
# MAGIC | **pdfplumber** | Good table extraction; layout analysis | Slower; per-document tuning | Workable, but needs tuning per BMA template |
# MAGIC | **Apache Tika** | Multi-format; metadata extraction | Java dependency; heavy | Overkill: silver keeps PDFs only |
# MAGIC | **Unstructured.io** | ML-powered; good chunking | External service; API costs | Sends regulatory documents outside the workspace |
# MAGIC
# MAGIC **AI Parse Documents** is used: it keeps parsing inside the workspace and governed by Unity Catalog.

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count, size, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.data_processor import DataProcessor

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
# 1. Removes parsed rows and chunks of papers silver no longer holds (filtered out or superseded)
# 2. Parses downloaded papers not yet parsed with ai_parse_document into silver_parsed_consultation_papers
# 3. Extracts and cleans text and table chunks, joins the paper metadata, embeds each chunk with the
#    configured embedding endpoint and appends them to gold_consultation_paper_chunks (Change Data Feed on)
# Each paper is parsed and embedded once, so re-runs only process papers downloaded since.

processor.process_and_save()

# COMMAND ----------
# Verify the Data

parsed_df = spark.table(processor.parsed_table)
chunks_df = spark.table(processor.chunks_table)

logger.info(f"Parsed papers: {parsed_df.count()}, chunks: {chunks_df.count()}")
chunks_df.printSchema()

logger.info("Chunks per paper:")
chunks_df.groupBy("document_id", "title").agg(count("*").alias("chunks")).orderBy(col("chunks").desc()).show(
    10, truncate=60
)

logger.info("Sample chunks:")
chunks_df.select("id", "page_id", "title", "text").show(5, truncate=80)

# COMMAND ----------
# Data Quality Checks

parsed_df.agg(
    spark_sum(when(col("parse_errors").isNotNull() & (col("parse_errors") != "[]"), 1).otherwise(0)).alias(
        "papers_with_page_errors"
    ),
).show(truncate=False)

# Parsed papers that yielded no chunks are retried by every run, so they show up here until fixed
parsed_df.join(chunks_df.select("document_id").distinct(), "document_id", "left_anti").select(
    "document_id", "volume_path"
).show(truncate=False)

chunks_df.agg(
    spark_sum(when(col("embedding").isNull(), 1).otherwise(0)).alias("missing_embeddings"),
    spark_sum(when(col("page_id").isNull(), 1).otherwise(0)).alias("missing_pages"),
    spark_sum(when(size("topics") == 0, 1).otherwise(0)).alias("untagged_topics"),
).show(truncate=False)

logger.info("Consultation paper processing completed")
