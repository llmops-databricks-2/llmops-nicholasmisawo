"""Column layouts of every Delta table, in pipeline order.

raw_fcr_search_log            one row per SerpAPI search          (SEARCH_LOG_SCHEMA)
raw_fcr_search_results        every FCR PDF found                 (SEARCH_RESULTS_SCHEMA)
raw_fcr_register              curated FCRs + where each file is   (REGISTER_SCHEMA)
raw_fcr_parsed_documents      ai_parse_document output            (PARSED_SCHEMA)

These are what Python lands; dbt builds everything after them (dbt/models).
"""

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
)

# Rows as each source returns them (see sources/)
SOURCE_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("title", StringType(), False),
        StructField("snippet", StringType(), True),
        StructField("pdf_url", StringType(), True),
        StructField("displayed_link", StringType(), True),
        StructField("site_name", StringType(), True),  # The result's site as Google names it
        StructField("domain", StringType(), True),
        StructField("search_queries", ArrayType(StringType()), True),  # Every query that found the URL, sorted
        StructField("search_rank", IntegerType(), True),  # Best position across those queries, 1-based
        StructField("source", StringType(), True),
        StructField("ingestion_timestamp", StringType(), True),  # Ingest run that last inserted or changed the row
    ]
)

# raw_fcr_search_log: one row per billed SerpAPI search, append-only; search_plan reads it to skip
# queries asked recently
SEARCH_LOG_SCHEMA = StructType(
    [
        StructField("query", StringType(), False),
        StructField("reason", StringType(), True),  # Why search_plan asked it: missing_year, institution, discovery
        StructField("page_start", IntegerType(), True),  # Result offset of the page: 0, 10, ...
        StructField("results", IntegerType(), True),
        StructField("fcr_results", IntegerType(), True),  # Results whose title, filename or snippet look like an FCR
        StructField("searched_at", StringType(), False),  # Ingest run that made the search (UTC ISO)
    ]
)

# Set by the search-results MERGE rather than by the source, so history survives re-runs
TRACKING_FIELDS = [
    StructField("first_seen_at", StringType(), True),  # Ingest run that first inserted the row
    StructField("is_listed", BooleanType(), True),  # False once the latest search stops returning the URL
]

# raw_fcr_search_results: every FCR PDF any run has found, by search or on a page, as the source returned it
SEARCH_RESULTS_SCHEMA = StructType([*SOURCE_SCHEMA.fields, *TRACKING_FIELDS])

# Curated metadata (see curation.curate), refreshed from the search results on every run
REGISTER_METADATA_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("source", StringType(), True),
        StructField("registered_name", StringType(), True),  # As the reference list gives it; None if not on it
        StructField("institution", StringType(), True),  # As the FCR's title gives it
        StructField("domain", StringType(), True),
        StructField("title", StringType(), False),
        StructField("doc_kind", StringType(), False),
        StructField("file_type", StringType(), True),
        StructField("file_url", StringType(), True),
        StructField("reporting_year", IntegerType(), True),
        StructField("snippet", StringType(), True),
        StructField("search_queries", ArrayType(StringType()), True),
        StructField("search_rank", IntegerType(), True),
        StructField("is_listed", BooleanType(), True),
        StructField("superseded_ids", ArrayType(StringType()), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("first_seen_at", StringType(), True),
    ]
)

# Owned by the download job; ingest inserts them as None and never overwrites them
STORAGE_FIELDS = [
    StructField("processed", LongType(), True),  # yyyyMMddHHmm, matching the course's arxiv_papers table
    StructField("volume_path", StringType(), True),
]

# raw_fcr_register: curated FCRs plus where each file landed in the volume
REGISTER_SCHEMA = StructType([*REGISTER_METADATA_SCHEMA.fields, *STORAGE_FIELDS])

# raw_fcr_parsed_documents: ai_parse_document output for each downloaded FCR, parsed once
PARSED_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("volume_path", StringType(), True),
        StructField("parsed_content", StringType(), True),  # ai_parse_document VARIANT as JSON
        StructField("parse_errors", StringType(), True),  # Per-page error_status as JSON; None when all pages parsed
        StructField("processed", LongType(), True),  # yyyyMMddHHmm of the run that parsed the file
    ]
)
