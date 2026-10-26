"""Column layouts of every Delta table, in pipeline order.

bronze_fcr_search_log         one row per SerpAPI search          (SEARCH_LOG_SCHEMA)
bronze_fcr_documents          every search result found           (BRONZE_SCHEMA)
silver_fcr_documents          curated FCRs + where each file is   (SILVER_SCHEMA)
silver_parsed_fcr_documents   ai_parse_document output            (PARSED_SCHEMA)
gold_fcr_chunks               cleaned chunks + FCR metadata       (CHUNKS_SCHEMA)
gold_fcr_chunks_by_section    the same, plus an embedding         (EMBEDDED_CHUNKS_SCHEMA)
"""

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    FloatType,
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

# bronze_fcr_search_log: one row per billed SerpAPI search, append-only; search_plan reads it to skip
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

# Set by the bronze MERGE rather than by the source, so history survives re-runs
BRONZE_TRACKING_FIELDS = [
    StructField("first_seen_at", StringType(), True),  # Ingest run that first inserted the row
    StructField("is_listed", BooleanType(), True),  # False once the latest search stops returning the URL
]

# bronze_fcr_documents: every search result any run has found, as the source returned it
BRONZE_SCHEMA = StructType([*SOURCE_SCHEMA.fields, *BRONZE_TRACKING_FIELDS])

# Curated metadata (see curation.curate), refreshed from bronze on every ingest
SILVER_METADATA_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("source", StringType(), True),
        StructField("institution", StringType(), True),
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

# silver_fcr_documents: curated FCRs plus where each file landed in the volume
SILVER_SCHEMA = StructType([*SILVER_METADATA_SCHEMA.fields, *STORAGE_FIELDS])

# silver_parsed_fcr_documents: ai_parse_document output for each downloaded silver FCR, parsed once
PARSED_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("volume_path", StringType(), True),
        StructField("parsed_content", StringType(), True),  # ai_parse_document VARIANT as JSON
        StructField("parse_errors", StringType(), True),  # Per-page error_status as JSON; None when all pages parsed
        StructField("processed", LongType(), True),  # yyyyMMddHHmm of the run that parsed the file
    ]
)

# gold_fcr_chunks: cleaned text chunks with the FCR's metadata, the RAG source; not embedded yet
CHUNKS_SCHEMA = StructType(
    [
        StructField("id", StringType(), False),  # <document_id>_<chunk_id>, the vector index primary key
        StructField("document_id", StringType(), False),
        StructField("chunk_id", StringType(), True),
        StructField("page_id", IntegerType(), True),  # Page the chunk starts on, for citations
        StructField("text", StringType(), True),
        StructField("title", StringType(), True),
        StructField("institution", StringType(), True),
        StructField("domain", StringType(), True),
        StructField("reporting_year", IntegerType(), True),
        StructField("file_url", StringType(), True),
        StructField("processed", LongType(), True),  # yyyyMMddHHmm of the run that chunked the document
    ]
)

# The chunks table kept for search also holds each chunk's embedding (None until embedded)
EMBEDDING_FIELD = StructField("embedding", ArrayType(FloatType()), True)
EMBEDDED_CHUNKS_SCHEMA = StructType([*CHUNKS_SCHEMA.fields, EMBEDDING_FIELD])
