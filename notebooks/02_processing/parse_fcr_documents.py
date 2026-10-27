# Databricks notebook source
# MAGIC %md
# MAGIC # Parse the downloaded FCR PDFs
# MAGIC
# MAGIC 1. Remove the parses of FCRs the register no longer holds
# MAGIC 2. Parse newly downloaded PDFs with `ai_parse_document` into `raw_fcr_parsed_documents`
# MAGIC
# MAGIC dbt does everything after this: section chunks, embeddings and the knowledge base (`dbt/models/context`).
# MAGIC
# MAGIC Why `ai_parse_document`:
# MAGIC
# MAGIC | Tool | Fit for Bermuda FCRs |
# MAGIC |------|---------------------|
# MAGIC | **AI Parse Documents** | Best: handles every company's layout and keeps the solvency and capital tables; runs in the workspace |
# MAGIC | PyPDF2 | Loses tables; poor with complex layouts |
# MAGIC | pdfplumber | Good tables, but needs tuning per template |
# MAGIC | Unstructured.io | Its accurate `hi_res` mode needs system packages (OCR, PDF tools) serverless can't install; the hosted API sends documents outside the workspace |

# COMMAND ----------

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, count

from fcr_curator.config import load_config
from fcr_curator.parsing import DocumentParser

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
parser = DocumentParser(spark=spark, config=cfg)

# COMMAND ----------
# 1-2. Parse what's new (each FCR is parsed once; re-runs only pick up new downloads)

parser.ensure_table()
parser.remove_dropped_documents()
logger.info(f"Parsed {parser.parse_pending_documents()} new FCRs into {parser.parsed_table}")

# COMMAND ----------
# Summary: FCRs whose parse reported page errors are worth a look

parsed_df = spark.table(parser.parsed_table)
logger.info(f"Parsed FCRs: {parsed_df.count()}")
parsed_df.agg(count(col("parse_errors")).alias("with_page_errors")).show()
