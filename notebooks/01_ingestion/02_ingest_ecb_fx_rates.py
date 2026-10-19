# Databricks notebook source

from datetime import UTC, datetime
from io import StringIO

import pandas as pd
import requests
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import (
    col,
    countDistinct,
    when,
)
from pyspark.sql.functions import (
    max as spark_max,
)
from pyspark.sql.functions import (
    min as spark_min,
)
from pyspark.sql.functions import (
    sum as spark_sum,
)
from pyspark.sql.types import (
    DateType,
    DoubleType,
    StringType,
    StructField,
    StructType,
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
TABLE_NAME = "forex_exchange_rates"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
logger.info(f"Schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------
# Fetch EUR/USD Exchange Rates from ECB API
# API: https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A
# Series represents USD per 1 EUR.

ECB_API = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"

START_DATE = "2024-01-01"
END_DATE = datetime.now(UTC).strftime("%Y-%m-%d")


def fetch_ecb_rates(
    start_date: str = START_DATE,
    end_date: str = END_DATE,
):
    """Fetch daily EUR/USD exchange rates from the ECB API."""
    params = {
        "startPeriod": start_date,
        "endPeriod": end_date,
        "format": "csvdata",
    }

    response = requests.get(
        ECB_API,
        params=params,
        headers={"Accept": "text/csv"},
        timeout=60,
    )
    response.raise_for_status()

    rates = pd.read_csv(StringIO(response.text))

    required = {"TIME_PERIOD", "OBS_VALUE"}
    missing = required - set(rates.columns)

    if missing:
        raise ValueError(f"Missing ECB API columns: {sorted(missing)}")

    rates = rates[["TIME_PERIOD", "OBS_VALUE"]].copy()

    rates["TIME_PERIOD"] = pd.to_datetime(rates["TIME_PERIOD"], errors="coerce")
    rates["OBS_VALUE"] = pd.to_numeric(rates["OBS_VALUE"], errors="coerce")

    rates = (
        rates.dropna(subset=["TIME_PERIOD", "OBS_VALUE"])
        .sort_values("TIME_PERIOD")
        .drop_duplicates(subset=["TIME_PERIOD"])
    )

    ingestion_time = datetime.now(UTC).isoformat()

    return [
        {
            "observation_date": row.TIME_PERIOD.date(),
            "currency_pair": "EUR/USD",
            "exchange_rate": float(row.OBS_VALUE),
            "base_currency": "EUR",
            "quote_currency": "USD",
            "source": "ecb",
            "series_key": "EXR/D.USD.EUR.SP00.A",
            "ingestion_timestamp": ingestion_time,
        }
        for row in rates.itertuples(index=False)
    ]


logger.info(f"Fetching ECB EUR/USD exchange rates from {START_DATE} to {END_DATE}...")

rates = fetch_ecb_rates(
    start_date=START_DATE,
    end_date=END_DATE,
)

if not rates:
    raise ValueError("No ECB exchange-rate observations returned")

logger.info(f"Fetched {len(rates)} observations")
logger.info(f"Sample observation: {rates[0]}")

# COMMAND ----------
# Create Delta Table in Unity Catalog

schema = StructType(
    [
        StructField("observation_date", DateType(), False),
        StructField("currency_pair", StringType(), False),
        StructField("exchange_rate", DoubleType(), False),
        StructField("base_currency", StringType(), False),
        StructField("quote_currency", StringType(), False),
        StructField("source", StringType(), True),
        StructField("series_key", StringType(), True),
        StructField("ingestion_timestamp", StringType(), True),
    ]
)

df = spark.createDataFrame(rates, schema=schema)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_path)

logger.info(f"Created Delta table: {table_path}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Data

rates_df = spark.table(table_path)

logger.info(f"Table: {table_path}")
logger.info(f"Total observations: {rates_df.count()}")
rates_df.printSchema()

logger.info("Sample records:")
rates_df.select(
    "observation_date",
    "currency_pair",
    "exchange_rate",
).show(10, truncate=False)

# COMMAND ----------
# Data Statistics

logger.info("Exchange rate summary:")

rates_df.selectExpr(
    "MIN(exchange_rate) AS minimum_rate",
    "MAX(exchange_rate) AS maximum_rate",
    "AVG(exchange_rate) AS average_rate",
    "MIN(observation_date) AS start_date",
    "MAX(observation_date) AS end_date",
).show(truncate=False)

logger.info("Observations by year:")

rates_df.selectExpr("YEAR(observation_date) AS year").groupBy("year").count().orderBy("year").show()

logger.info("Most recent exchange rates:")

rates_df.select(
    "observation_date",
    "exchange_rate",
).orderBy(col("observation_date").desc()).show(10, truncate=False)

# COMMAND ----------
# Data Quality Checks

total_count = rates_df.count()

quality_df = rates_df.agg(
    spark_sum(when(col("observation_date").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("exchange_rate").isNull(), 1).otherwise(0)).alias("missing_rates"),
    spark_sum(when(col("exchange_rate") <= 0, 1).otherwise(0)).alias("invalid_rates"),
    countDistinct("observation_date").alias("unique_dates"),
    spark_min("observation_date").alias("first_date"),
    spark_max("observation_date").alias("last_date"),
)

logger.info(f"Total observations: {total_count}")
quality_df.show(truncate=False)

# COMMAND ----------
# Exchange Rates Ready for Analytics

logger.info("EUR/USD observations ready for analysis:")

rates_df.select(
    "observation_date",
    "currency_pair",
    "exchange_rate",
    "source",
).orderBy(col("observation_date").desc()).show(10, truncate=False)

logger.info("ECB EUR/USD ingestion completed")
