# Databricks notebook source
# MAGIC %md
# MAGIC # Process: parse FCR PDFs and split them into chunks
# MAGIC
# MAGIC 1. Remove parsed rows and chunks of FCRs silver no longer has
# MAGIC 2. Parse newly downloaded PDFs with `ai_parse_document` into `silver_parsed_fcr_documents`
# MAGIC 3. Chunk every parsed FCR with each strategy in `chunking.py`, adding the FCR's metadata:
# MAGIC    - `gold_fcr_chunks`: one chunk per text or table element
# MAGIC    - `gold_fcr_chunks_by_section`: one chunk per section, size-limited (like Unstructured's `by_title`)
# MAGIC 4. Embed the section chunks (the strategy kept for search) through the Unity Gateway
# MAGIC 5. Compare the two strategies side by side
# MAGIC
# MAGIC Why `ai_parse_document`:
# MAGIC
# MAGIC | Tool | Fit for Bermuda FCRs |
# MAGIC |------|---------------------|
# MAGIC | **AI Parse Documents** | Best: handles every insurer's layout and keeps the solvency and capital tables; runs in the workspace |
# MAGIC | PyPDF2 | Loses tables; poor with complex layouts |
# MAGIC | pdfplumber | Good tables, but needs tuning per insurer template |
# MAGIC | Unstructured.io | Open-source library runs locally, but its accurate `hi_res` mode needs system packages (OCR, PDF tools) serverless can't install; the hosted API sends documents outside the workspace |

# COMMAND ----------

from functools import reduce

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import avg, col, count, length, lit, percentile_approx, when
from pyspark.sql.functions import max as spark_max
from pyspark.sql.functions import round as spark_round
from pyspark.sql.functions import sum as spark_sum

from fcr_curator.config import load_config
from fcr_curator.data_processor import DataProcessor
from fcr_curator.gateway import gateway_client, gateway_embedder

# COMMAND ----------
# Settings

spark = SparkSession.builder.getOrCreate()
dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
# SQL ai_query can't call the course's embedding service, so embeddings go through the Unity Gateway API
embed = gateway_embedder(gateway_client(WorkspaceClient()), cfg.embedding_endpoint)
processor = DataProcessor(spark=spark, config=cfg, embed=embed)

# COMMAND ----------
# 1-4. Parse, chunk and embed (each FCR and chunk is handled once; re-runs only pick up new downloads)

processor.process_and_save()

# COMMAND ----------
# 5. Compare the chunking strategies side by side

chunks_by_strategy = reduce(
    DataFrame.unionByName,
    [
        spark.table(table)
        .select("document_id", "chunk_id", "page_id", "text")
        .withColumn("strategy", lit(table.split(".")[-1]))
        for table in processor.chunk_tables
    ],
).withColumn("chars", length("text"))

# Size per strategy, in characters. Under 50 is usually noise; over 2,000 (~500 tokens) is too broad to retrieve well
chunks_by_strategy.groupBy("strategy").agg(
    count("*").alias("chunks"),
    spark_round(avg("chars")).alias("avg"),
    percentile_approx("chars", 0.5).alias("median"),
    percentile_approx("chars", 0.9).alias("p90"),
    spark_max("chars").alias("max"),
    spark_sum(when(col("chars") < 50, 1).otherwise(0)).alias("under_50"),
    spark_sum(when(col("chars") > 2000, 1).otherwise(0)).alias("over_2000"),
).orderBy("strategy").show(truncate=False)

# The same FCR under each strategy: its first chunks, in order
sample_id = spark.table(processor.parsed_table).select("document_id").orderBy("document_id").first()[0]
(
    chunks_by_strategy.where(col("document_id") == sample_id)
    .orderBy("strategy", "page_id", col("chunk_id").cast("int"))
    .select("strategy", "page_id", "chars", "text")
    .show(30, truncate=120)
)

# Every chunk kept for search should now have an embedding
spark.table(processor.search_table).select(
    count("*").alias("chunks"), spark_sum(when(col("embedding").isNull(), 1).otherwise(0)).alias("not_embedded")
).show()
