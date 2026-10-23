# Databricks notebook source

import os
import re
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from databricks.sdk.runtime import dbutils
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, explode, floor, when
from pyspark.sql.functions import sum as spark_sum
from pyspark.sql.types import ArrayType, BooleanType, LongType, StringType, StructField, StructType
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from consultation_curator.config import load_config

# COMMAND ----------
# Create Spark session and load configuration

spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

CATALOG = cfg.catalog
SCHEMA = cfg.schema
VOLUME = cfg.volume
TABLE_NAME = "bma_consultation_papers"
table_path = f"{CATALOG}.{SCHEMA}.{TABLE_NAME}"
volume_dir = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}/bma"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`.`{VOLUME}`")
os.makedirs(volume_dir, exist_ok=True)
logger.info(f"Schema {CATALOG}.{SCHEMA} and volume {volume_dir} ready")

# COMMAND ----------
# Fetch BMA Consultation Paper Metadata
# Source: https://www.bma.bm/documents-centre/documents-policy-and-guidance
# The page's filter panel posts to a `fetchdata` endpoint that returns rendered HTML cards.
# Cards carry no type/category/topic labels, so documents are tagged by querying once per filter value.

BMA_PAGE = "https://www.bma.bm/documents-centre/documents-policy-and-guidance"
BMA_FETCH = f"{BMA_PAGE}/documents-policy-and-guidance/fetchdata"

DOCUMENT_TYPE = ("9", "Regulatory Legislative Documents")
CATEGORIES = {"14": "Discussion/Consultation Papers", "103": "Thematic Review"}
INCLUDE_ARCHIVE = True
PAGE_SIZE = 50
MAX_RESULTS = None  # None = keep everything; set an int to cap the most recent documents

DATE_PATTERN = re.compile(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b")
TOPIC_INPUTS = ["parentsector[]", "childsector[]", "subchildsector[]"]


def create_session():
    """Create HTTP session with retries."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,  # BMA filters are POSTs; retry them too
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "ConsultationPaperCurator/1.0"})
    return session


def parse_date(text):
    """Parse the first 'DD Month YYYY' date in text into a date."""
    if not text:
        return None

    match = DATE_PATTERN.search(text)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(), "%d %B %Y").date()
    except ValueError:
        return None


def get_csrf_token(session):
    """Open the documents page to get a CSRF token and the topic id -> label map."""
    response = session.get(BMA_PAGE, timeout=60)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    token = soup.find("meta", attrs={"name": "csrf-token"})["content"]

    topics = {}
    for name in TOPIC_INPUTS:
        for checkbox in soup.find_all("input", attrs={"name": name}):
            label = checkbox.find_parent("label") or checkbox.find_next("label")
            if label:
                topics[checkbox["value"]] = re.sub(r"\s+", " ", label.get_text(" ", strip=True))

    return token, topics


def fetch_page(session, token, categories, topics=None, archive=False, page=1):
    """Fetch one page of filtered results from the BMA documents centre."""
    response = session.post(
        BMA_FETCH,
        data={
            "limit": PAGE_SIZE,
            "pageNumber": page,
            "types[]": [DOCUMENT_TYPE[0]],
            "sectors[]": list(categories),  # the site sends categories as `sectors`
            "topic[]": list(topics or []),
            "archive": "Y" if archive else "N",
            "isParentsectorClicked": "false",
            "sortDirection": "desc",
        },
        headers={"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest"},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("html") or "", int(payload.get("totalcount") or 0)


def parse_cards(html):
    """Parse document cards from a fetchdata HTML fragment."""
    soup = BeautifulSoup(html, "html.parser")
    documents = []

    for card in soup.select("li.document-column"):
        title_link = card.select_one("h3.d-title a[href]")
        id_link = card.select_one("a[data-viewid]")

        if title_link is None or id_link is None:
            continue

        pdf_url = title_link["href"]
        view_link = card.select_one('a[data-viewtype="view"]')
        download_link = card.select_one('a[data-viewtype="download"]')
        summary_tag = card.select_one("div.d-desc > p")
        date_tag = card.select_one("div.date")

        summary = summary_tag.get_text(" ", strip=True) if summary_tag else None
        uploaded = parse_date(date_tag.get_text(" ", strip=True) if date_tag else None)
        deadline = parse_date(summary)
        title = re.sub(r"\s+", " ", title_link.get_text(" ", strip=True))

        related = [link["href"] for link in card.select('a[href^="https://cdn.bma.bm/"]') if link["href"] != pdf_url]

        documents.append(
            {
                "document_id": f"bma_{id_link['data-viewid']}",
                "title": title,
                "summary": summary,
                "comment_deadline": deadline.isoformat() if deadline else None,
                "published": int(uploaded.strftime("%Y%m%d")) if uploaded else None,  # Long to match arXiv
                "uploaded_date": uploaded.isoformat() if uploaded else None,
                "document_type": DOCUMENT_TYPE[1],
                "is_stakeholder_letter": title.lower().startswith("stakeholder letter"),
                "pdf_url": pdf_url,
                "view_url": view_link["href"] if view_link else None,
                "download_url": download_link["href"] if download_link else None,
                "related_documents": list(dict.fromkeys(related)),
            }
        )

    return documents


def fetch_all(session, token, categories, topics=None, archive=False):
    """Page through every result for one filter combination."""
    documents = []
    page = 1

    while True:
        html, total = fetch_page(session, token, categories, topics, archive, page)
        batch = parse_cards(html)
        documents.extend(batch)

        if not batch or len(documents) >= total:
            return documents

        page += 1
        time.sleep(0.5)


def fetch_bma_documents(
    categories: dict = CATEGORIES,
    include_archive: bool = INCLUDE_ARCHIVE,
    max_results: int | None = MAX_RESULTS,
):
    """Fetch BMA consultation documents tagged with category, topic and archive status."""
    documents = {}
    archive_flags = [False, True] if include_archive else [False]

    with create_session() as session:
        token, topic_labels = get_csrf_token(session)

        # Base pass: one query per category and archive flag
        for category_id, category_name in categories.items():
            for archived in archive_flags:
                for doc in fetch_all(session, token, [category_id], archive=archived):
                    record = documents.setdefault(
                        doc["document_id"],
                        {**doc, "categories": [], "topics": [], "is_archived": archived},
                    )
                    if category_name not in record["categories"]:
                        record["categories"].append(category_name)

                logger.info(f"Category '{category_name}' (archived={archived}): {len(documents)} documents so far")

        # Topic pass: one query per topic, restricted to the same categories
        for topic_id, topic_name in topic_labels.items():
            for archived in archive_flags:
                for doc in fetch_all(session, token, categories.keys(), [topic_id], archive=archived):
                    record = documents.get(doc["document_id"])
                    if record and topic_name not in record["topics"]:
                        record["topics"].append(topic_name)

        logger.info(f"Tagged documents across {len(topic_labels)} topics")

    ingested_at = datetime.now(UTC).isoformat()
    publications = sorted(documents.values(), key=lambda doc: doc["published"] or 0, reverse=True)

    for doc in publications:
        doc.update(
            {
                "institution": "BMA",
                "source": "bma",
                "ingestion_timestamp": ingested_at,
                "processed": None,  # Set when the PDF lands in the volume
                "volume_path": None,  # Set when the PDF lands in the volume
            }
        )

    return publications[:max_results] if max_results else publications


logger.info("Fetching BMA consultation papers...")
publications = fetch_bma_documents()

if not publications:
    raise ValueError("No BMA consultation papers found")

logger.info(f"Fetched {len(publications)} documents")
logger.info(f"Sample: {publications[0]['title']}")
logger.info(f"Uploaded: {publications[0]['uploaded_date']}")
logger.info(f"Categories: {publications[0]['categories']}")
logger.info(f"Topics: {publications[0]['topics']}")
logger.info(f"PDF URL: {publications[0]['pdf_url']}")

# COMMAND ----------
# Download Documents to the Unity Catalog Volume

MAX_DOWNLOADS = 20  # Capped while testing; None = download everything


def download_document(session, url, path):
    """Stream a document to the volume; return True on success."""
    try:
        with session.get(url, timeout=120, stream=True) as response:
            response.raise_for_status()
            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        return True
    except (requests.RequestException, OSError) as exc:
        logger.warning(f"Failed to download {url}: {exc}")
        if os.path.exists(path):
            os.remove(path)
        return False


downloaded = skipped = failed = 0

with create_session() as session:
    for doc in publications[:MAX_DOWNLOADS]:
        # A few BMA "documents" are spreadsheets, so keep the source file's extension
        extension = PurePosixPath(urlparse(doc["pdf_url"]).path).suffix.lower() or ".pdf"
        path = f"{volume_dir}/{doc['document_id']}{extension}"

        if os.path.exists(path):
            skipped += 1
        elif download_document(session, doc["pdf_url"], path):
            downloaded += 1
            time.sleep(1)
        else:
            failed += 1
            continue

        doc["volume_path"] = path
        doc["processed"] = int(datetime.now(UTC).strftime("%Y%m%d%H%M"))  # Long to match arXiv

logger.info(f"Documents downloaded: {downloaded}, already present: {skipped}, failed: {failed}")

# COMMAND ----------
# Create Delta Table in Unity Catalog

schema = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("title", StringType(), False),
        StructField("summary", StringType(), True),
        StructField("comment_deadline", StringType(), True),
        StructField("published", LongType(), True),  # Long to match arXiv (yyyyMMdd)
        StructField("uploaded_date", StringType(), True),
        StructField("document_type", StringType(), True),
        StructField("categories", ArrayType(StringType()), True),
        StructField("topics", ArrayType(StringType()), True),
        StructField("is_archived", BooleanType(), True),
        StructField("is_stakeholder_letter", BooleanType(), True),
        StructField("pdf_url", StringType(), True),
        StructField("view_url", StringType(), True),
        StructField("download_url", StringType(), True),
        StructField("related_documents", ArrayType(StringType()), True),
        StructField("institution", StringType(), True),
        StructField("source", StringType(), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("processed", LongType(), True),
        StructField("volume_path", StringType(), True),
    ]
)

df = spark.createDataFrame(publications, schema=schema)

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
papers_df.groupBy(floor(col("published") / 10000).alias("year")).count().orderBy("year", ascending=False).show(30)

logger.info("Most recent documents:")
papers_df.select("title", "uploaded_date", "comment_deadline").orderBy(col("published").desc_nulls_last()).show(
    10, truncate=60
)

# COMMAND ----------
# Data Quality Checks

quality_df = papers_df.agg(
    spark_sum(when(col("published").isNull(), 1).otherwise(0)).alias("missing_dates"),
    spark_sum(when(col("pdf_url").isNull(), 1).otherwise(0)).alias("missing_pdf_urls"),
    spark_sum(when(col("volume_path").isNull(), 1).otherwise(0)).alias("missing_downloads"),
    spark_sum(when(col("topics").getItem(0).isNull(), 1).otherwise(0)).alias("untagged_topics"),
)

quality_df.show(truncate=False)

logger.info("BMA consultation paper ingestion completed")
