# Databricks notebook source

from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, explode, substring, when
from pyspark.sql.functions import sum as spark_sum

from consultation_paper_curator.config import load_config
from consultation_paper_curator.http_client import create_session
from consultation_paper_curator.schemas import RAW_SCHEMA
from consultation_paper_curator.sources.bma import fetch_bma_documents

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
TABLE_NAME = "bma_consultation_papers"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
logger.info(f"Schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------
# Fetch BMA Consultation Paper Metadata (no files are downloaded here; see 02_curate)

MAX_RESULTS = None  # None = keep everything; set an int to cap the most recent documents

logger.info("Fetching BMA consultation papers...")
with create_session() as session:
    publications = fetch_bma_documents(session, max_results=MAX_RESULTS)

if not publications:
    raise ValueError("No BMA consultation papers found")

logger.info(f"Fetched {len(publications)} documents")
logger.info(f"Sample: {publications[0]['title']}")
logger.info(f"Uploaded: {publications[0]['uploaded_date']}")
logger.info(f"Categories: {publications[0]['categories']}")
logger.info(f"Topics: {publications[0]['topics']}")
logger.info(f"PDF URL: {publications[0]['pdf_url']}")

# COMMAND ----------
# Create Delta Table in Unity Catalog

df = spark.createDataFrame(publications, schema=RAW_SCHEMA)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_path)

logger.info(f"Created Delta table: {table_path}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Data

papers_df = spark.table(table_path)

logger.info(f"Table: {table_path}")
logger.info(f"Total documents: {papers_df.count()}")
papers_df.printSchema()

logger.info("Sample records:")
papers_df.select("document_id", "title", "uploaded_date", "topics").show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Documents by category:")
papers_df.select(explode("categories").alias("category")).groupBy("category").count().orderBy(
    "count", ascending=False
).show(truncate=False)

logger.info("Documents by topic:")
papers_df.select(explode("topics").alias("topic")).groupBy("topic").count().orderBy("count", ascending=False).show(
    50, truncate=False
)

logger.info("Current vs archived:")
papers_df.groupBy("is_archived").count().show()

logger.info("Documents by year:")
papers_df.groupBy(substring("uploaded_date", 1, 4).alias("year")).count().orderBy("year", ascending=False).show(30)

logger.info("Most recent documents:")
papers_df.select("title", "uploaded_date", "comment_deadline").orderBy(col("uploaded_date").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Data Quality Checks

quality_df = papers_df.agg(
    spark_sum(when(col("uploaded_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdf_urls"),
    spark_sum(when(col("topics").getItem(0).isNull(), 1).otherwise(0)).alias("untagged_topics"),
)

quality_df.show(truncate=False)

logger.info("BMA consultation paper ingestion completed")
