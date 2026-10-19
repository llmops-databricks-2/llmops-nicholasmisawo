# Databricks notebook source

import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, length, when
from pyspark.sql.functions import sum as spark_sum
from pyspark.sql.types import LongType, StringType, StructField, StructType
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fx_market_intel.config import load_config

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
TABLE_NAME = "ecb_economic_bulletins"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
logger.info(f"Schema {CATALOG}.{SCHEMA} ready")

# COMMAND ----------
# Fetch ECB Economic Bulletin Publications
# Source: https://www.ecb.europa.eu/press/economic-bulletin/html/all_releases.en.html

ECB_ARCHIVE = "https://www.ecb.europa.eu/press/economic-bulletin/html/all_releases.en.html"

START_YEAR = 2024
MAX_RESULTS = 30

ISSUE_PATTERN = re.compile(
    r"eb(\d{4})(\d{2})\.en\.html$",
    re.IGNORECASE,
)

DATE_PATTERN = re.compile(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b")


def create_session():
    """Create HTTP session with retries."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "MarketIntelligenceResearch/1.0"})
    return session


def get_publication_date(link):
    """Extract publication date preceding an archive link."""
    for item in link.previous_elements:
        text = str(item).strip()
        match = DATE_PATTERN.search(text)

        if match:
            try:
                return datetime.strptime(match.group(), "%d %B %Y").date().isoformat()
            except ValueError:
                pass

    return None


def extract_pdf_url(soup, page_url):
    """Find the main Economic Bulletin PDF."""
    for link in soup.select("a[href]"):
        url = urljoin(page_url, link["href"])
        path = urlparse(url).path.lower()
        label = link.get_text(" ", strip=True).lower()

        if not path.endswith(".pdf"):
            continue

        if "annex" in path or "annex" in label:
            continue

        if "economic bulletin" in label or re.search(r"ecb\.eb\d{6}", path):
            return url

    return None


def extract_text(soup):
    """Extract publication text from the HTML page."""
    content = soup.find("main") or soup.find("article")

    if content is None:
        return None

    for element in content.select("script, style, nav, footer, noscript"):
        element.decompose()

    text = content.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip() or None


def fetch_ecb_bulletins(
    start_year: int = START_YEAR,
    max_results: int = MAX_RESULTS,
):
    """Discover and retrieve ECB Economic Bulletins."""
    publications = []
    seen_ids = set()

    with create_session() as session:
        response = session.get(ECB_ARCHIVE, timeout=60)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.select("a[href]"):
            url = urljoin(ECB_ARCHIVE, link["href"])
            match = ISSUE_PATTERN.search(urlparse(url).path)

            if not match:
                continue

            year, issue = map(int, match.groups())
            document_id = f"ecb_eb_{year}_{issue:02d}"

            if year < start_year or document_id in seen_ids:
                continue

            seen_ids.add(document_id)

            published = get_publication_date(link)

            try:
                report_response = session.get(url, timeout=60)
                report_response.raise_for_status()
            except requests.RequestException as exc:
                logger.warning(f"Skipping {document_id}: {exc}")
                continue

            report_soup = BeautifulSoup(report_response.text, "html.parser")

            text = extract_text(report_soup)
            pdf_url = extract_pdf_url(report_soup, url)

            if not text or len(text) < 200:
                logger.warning(f"Insufficient text for {document_id}")
                continue

            publications.append(
                {
                    "document_id": document_id,
                    "title": f"ECB Economic Bulletin Issue {issue}, {year}",
                    "year": year,
                    "issue_number": issue,
                    "published": published,
                    "document_type": "Economic Bulletin",
                    "institution": "ECB",
                    "summary": text[:1000],
                    "full_text": text,
                    "html_url": url,
                    "pdf_url": pdf_url,
                    "source": "ecb",
                    "ingestion_timestamp": datetime.now(UTC).isoformat(),
                    "processed": None,  # Will be set in Lecture 2.2
                    "volume_path": None,  # Will be set in Lecture 2.2
                }
            )

            logger.info(f"Fetched {document_id} | Text: {len(text)} chars | PDF: {bool(pdf_url)}")

            if len(publications) >= max_results:
                break

    return publications


logger.info("Fetching ECB Economic Bulletins...")

publications = fetch_ecb_bulletins(
    start_year=START_YEAR,
    max_results=MAX_RESULTS,
)

if not publications:
    raise ValueError("No ECB Economic Bulletins found")

logger.info(f"Fetched {len(publications)} publications")
logger.info(f"Sample: {publications[0]['title']}")
logger.info(f"HTML URL: {publications[0]['html_url']}")
logger.info(f"PDF URL: {publications[0]['pdf_url']}")

# COMMAND ----------
# Create Delta Table in Unity Catalog

schema = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("title", StringType(), False),
        StructField("year", LongType(), True),
        StructField("issue_number", LongType(), True),
        StructField("published", StringType(), True),
        StructField("document_type", StringType(), True),
        StructField("institution", StringType(), True),
        StructField("summary", StringType(), True),
        StructField("full_text", StringType(), True),
        StructField("html_url", StringType(), True),
        StructField("pdf_url", StringType(), True),
        StructField("source", StringType(), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("processed", LongType(), True),  # Will be set in Lecture 2.2
        StructField("volume_path", StringType(), True),  # Will be set in Lecture 2.2
    ]
)

df = spark.createDataFrame(publications, schema=schema)

df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_path)

logger.info(f"Created Delta table: {table_path}")
logger.info(f"Records: {df.count()}")

# COMMAND ----------
# Verify the Data

bulletins_df = spark.table(table_path)

logger.info(f"Table: {table_path}")
logger.info(f"Total bulletins: {bulletins_df.count()}")
bulletins_df.printSchema()

logger.info("Sample records:")
bulletins_df.select(
    "document_id",
    "title",
    "published",
    "pdf_url",
).show(10, truncate=60)

# COMMAND ----------
# Data Statistics

logger.info("Bulletins by year:")
bulletins_df.groupBy("year").count().orderBy("year").show()

logger.info("Most recent bulletins:")
bulletins_df.select("title", "published", "document_id").orderBy(col("published").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Data Quality Checks

quality_df = bulletins_df.agg(
    spark_sum(when(col("published").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("full_text").isNull() | (length("full_text") == 0), 1).otherwise(0)).alias("missing_text"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdfs"),
)

quality_df.show(truncate=False)

# COMMAND ----------
# Publications Ready for Processing (Lecture 2.2)

logger.info("ECB bulletins ready for processing:")

bulletins_df.select(
    "document_id",
    "title",
    "html_url",
    "pdf_url",
    "processed",  # Will be set in Lecture 2.2
    "volume_path",  # Will be set in Lecture 2.2
).show(10, truncate=60)

logger.info("ECB Economic Bulletin ingestion completed")
