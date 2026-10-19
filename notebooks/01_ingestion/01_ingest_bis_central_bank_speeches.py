# Databricks notebook source

import hashlib
import io
import zipfile
from datetime import UTC, datetime

import pandas as pd
import requests
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, length, size, when
from pyspark.sql.functions import sum as spark_sum
from pyspark.sql.types import (
    ArrayType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from fx_market_intel.config import load_config

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
TABLE_NAME = "central_bank_speeches"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
logger.info(f"Schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------
# Fetch BIS Central Bank Speeches
# Source: https://www.bis.org/cbspeeches/download.htm

YEARS = [2024, 2025, 2026]


def load_bis_speeches(years: list[int]) -> pd.DataFrame:
    """Download BIS central bank speeches from the official ZIP archives."""
    datasets = []

    with requests.Session() as session:
        for year in years:
            url = f"https://www.bis.org/pages/download-central-bankers-speeches/speeches-{year}.zip"

            logger.info(f"Downloading BIS speeches for {year}")

            response = session.get(url, timeout=120)
            response.raise_for_status()

            archive_bytes = io.BytesIO(response.content)

            if not zipfile.is_zipfile(archive_bytes):
                raise ValueError(
                    f"Invalid BIS ZIP archive for {year}: "
                    f"status={response.status_code}, "
                    f"content_type={response.headers.get('Content-Type')}"
                )

            archive_bytes.seek(0)

            with zipfile.ZipFile(archive_bytes) as archive:
                csv_files = [name for name in archive.namelist() if name.lower().endswith(".csv")]

                if not csv_files:
                    raise ValueError(f"No CSV files found for {year}")

                for filename in csv_files:
                    with archive.open(filename) as file:
                        datasets.append(pd.read_csv(file))

            logger.info(f"Loaded BIS speeches for {year}")

    if not datasets:
        raise ValueError("No BIS speeches were downloaded")

    return pd.concat(datasets, ignore_index=True)


# COMMAND ----------
# Inspect BIS Raw Data (Temporary Debugging)

raw_speeches = load_bis_speeches([2024])

print("Available columns:")
print(raw_speeches.columns.tolist())

print("Sample authors and titles:")
print(raw_speeches[["author", "title"]].head(20))


# COMMAND ----------

# Speaker mapping for the initial ECB/Fed research scope.
# Extend as additional speakers are verified.
ECB_SPEAKERS = {
    "christine lagarde",
    "luis de guindos",
    "philip r lane",
    "fabio panetta",
    "isabel schnabel",
    "frank eldrson",
}

FED_SPEAKERS = {
    "jerome h powell",
    "jerome powell",
    "lael brainard",
    "john c williams",
    "john williams",
    "christopher j waller",
    "christopher waller",
    "michelle w bowman",
    "michelle bowman",
}


def normalize_text(value):
    """Normalize optional speech metadata."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def normalize_name(value):
    """Normalize speaker names for matching."""
    import re

    name = (normalize_text(value) or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def classify_institution(author):
    """Classify verified ECB/Fed speakers."""
    name = normalize_name(author)
    if name in ECB_SPEAKERS:
        return "ECB"
    if name in FED_SPEAKERS:
        return "FED"
    return None


def fetch_bis_speeches(
    years: list[int] = YEARS,
    institutions: tuple[str, ...] = ("ECB", "FED"),
    max_results: int | None = None,
):
    """Fetch BIS speeches and retain selected institutions."""
    speeches = load_bis_speeches(years)

    required = {"url", "title", "description", "date", "text", "author"}
    missing = required - set(speeches.columns)

    if missing:
        raise ValueError(f"Missing BIS columns: {sorted(missing)}")

    speeches = speeches.copy()
    speeches["institution"] = speeches["author"].apply(classify_institution)

    if institutions:
        speeches = speeches[speeches["institution"].isin(institutions)]

    speeches["date"] = pd.to_datetime(speeches["date"], errors="coerce", utc=True)

    speeches = speeches.dropna(subset=["date"]).sort_values("date", ascending=False).drop_duplicates(subset=["url"])

    if max_results is not None:
        speeches = speeches.head(max_results)

    records = []

    for row in speeches.itertuples(index=False):
        url = normalize_text(row.url)
        title = normalize_text(row.title)
        speech_text = normalize_text(row.text)

        if not url or not title or not speech_text:
            continue

        speech_id = hashlib.sha256(url.encode("utf-8")).hexdigest()

        records.append(
            {
                "speech_id": speech_id,
                "title": title,
                "authors": [normalize_text(row.author)] if normalize_text(row.author) else [],
                "summary": normalize_text(row.description),
                "published": row.date.to_pydatetime(),
                "institution": row.institution,
                "speech_text": speech_text,
                "speech_url": url,
                "source": "bis",
                "ingestion_timestamp": datetime.now(UTC).isoformat(),
                "processed": None,  # Will be set in Lecture 2.2
                "volume_path": None,  # Will be set in Lecture 2.2
            }
        )

    return records


logger.info(f"Fetching BIS speeches for {YEARS}...")

speeches = fetch_bis_speeches(
    years=YEARS,
    institutions=("ECB", "FED"),
)

if not speeches:
    raise ValueError("No matching BIS speeches found. Check the speaker mapping.")

logger.info(f"Fetched {len(speeches)} speeches")
logger.info(f"Sample title: {speeches[0]['title']}")
logger.info(f"Authors: {speeches[0]['authors']}")
logger.info(f"Institution: {speeches[0]['institution']}")
logger.info(f"Speech URL: {speeches[0]['speech_url']}")

# COMMAND ----------
# Create Delta Table in Unity Catalog

schema = StructType(
    [
        StructField("speech_id", StringType(), False),
        StructField("title", StringType(), False),
        StructField("authors", ArrayType(StringType()), True),
        StructField("summary", StringType(), True),
        StructField("published", TimestampType(), True),
        StructField("institution", StringType(), True),
        StructField("speech_text", StringType(), True),
        StructField("speech_url", StringType(), True),
        StructField("source", StringType(), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("processed", LongType(), True),  # Will be set in Lecture 2.2
        StructField("volume_path", StringType(), True),  # Will be set in Lecture 2.2
    ]
)

df = spark.createDataFrame(speeches, schema=schema)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_path)

logger.info(f"Created Delta table: {table_path}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Data

speeches_df = spark.table(table_path)

logger.info(f"Table: {table_path}")
logger.info(f"Total speeches: {speeches_df.count()}")
speeches_df.printSchema()

logger.info("Sample records:")
speeches_df.select("speech_id", "title", "institution", "authors", "published").show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Speeches by institution:")
speeches_df.groupBy("institution").count().orderBy("count", ascending=False).show()

logger.info("Most recent speeches:")
speeches_df.select("title", "institution", "published").orderBy(col("published").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Data Quality Checks

quality_df = speeches_df.agg(
    spark_sum(when(col("authors").isNull() | (size("authors") == 0), 1).otherwise(0)).alias("missing_authors"),
    spark_sum(when(col("summary").isNull() | (length("summary") == 0), 1).otherwise(0)).alias("missing_summaries"),
    spark_sum(when(col("published").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("speech_text").isNull() | (length("speech_text") == 0), 1).otherwise(0)).alias("missing_text"),
)

quality_df.show(truncate=False)

# COMMAND ----------
# Speeches Ready for Processing (Lecture 2.2)

logger.info("Speeches ready for processing:")

speeches_df.select(
    "speech_id",
    "title",
    "institution",
    "speech_text",
    "processed",  # Will be set in Lecture 2.2
    "volume_path",  # Will be set in Lecture 2.2
).show(5, truncate=60)

logger.info("BIS speeches ingestion completed")
