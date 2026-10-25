"""Spark schemas of the project's medallion Delta tables, shared by the notebooks that write and read them."""

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    FloatType,
    IntegerType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# Rows as each source returns them (see sources/)
SOURCE_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("title", StringType(), False),
        StructField("summary", StringType(), True),
        StructField("comment_deadline", StringType(), True),
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
        StructField("ingestion_timestamp", StringType(), True),  # Ingest run that last inserted or changed the row
    ]
)

# Set by the bronze MERGE rather than by the source, so history survives re-runs
BRONZE_TRACKING_FIELDS = [
    StructField("first_seen_at", StringType(), True),  # Ingest run that first inserted the row
    StructField("is_listed", BooleanType(), True),  # False once the source stops listing the document
]

# bronze_consultation_papers: every document any source has listed, as it returned them
BRONZE_SCHEMA = StructType([*SOURCE_SCHEMA.fields, *BRONZE_TRACKING_FIELDS])

# Curated metadata (see curation.curate), refreshed from bronze on every ingest
SILVER_METADATA_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("source", StringType(), True),
        StructField("institution", StringType(), True),
        StructField("title", StringType(), False),
        StructField("doc_kind", StringType(), False),
        StructField("file_type", StringType(), True),
        StructField("file_url", StringType(), True),
        StructField("published_date", DateType(), True),
        StructField("listed_date", DateType(), True),
        StructField("file_uploaded_at", TimestampType(), True),
        StructField("comment_deadline", DateType(), True),
        StructField("submission_note", StringType(), True),
        StructField("submission_emails", ArrayType(StringType()), True),
        StructField("topics", ArrayType(StringType()), True),
        StructField("is_archived", BooleanType(), True),
        StructField("is_listed", BooleanType(), True),
        StructField("related_documents", ArrayType(StringType()), True),
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

# silver_consultation_papers: curated papers plus where each file landed in the volume
SILVER_SCHEMA = StructType([*SILVER_METADATA_SCHEMA.fields, *STORAGE_FIELDS])

# silver_parsed_consultation_papers: ai_parse_document output for each downloaded silver paper, parsed once
PARSED_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("volume_path", StringType(), True),
        StructField("parsed_content", StringType(), True),  # ai_parse_document VARIANT as JSON
        StructField("parse_errors", StringType(), True),  # Per-page error_status as JSON; None when all pages parsed
        StructField("processed", LongType(), True),  # yyyyMMddHHmm of the run that parsed the file
    ]
)

# gold_consultation_paper_chunks: cleaned text chunks with the paper's metadata and an embedding, the RAG source
CHUNKS_SCHEMA = StructType(
    [
        StructField("id", StringType(), False),  # <document_id>_<chunk_id>, the vector index primary key
        StructField("document_id", StringType(), False),
        StructField("chunk_id", StringType(), True),
        StructField("page_id", IntegerType(), True),  # Page the chunk starts on, for citations
        StructField("text", StringType(), True),
        StructField("title", StringType(), True),
        StructField("doc_kind", StringType(), True),
        StructField("institution", StringType(), True),
        StructField("published_date", DateType(), True),
        StructField("comment_deadline", DateType(), True),
        StructField("topics", ArrayType(StringType()), True),
        StructField("is_archived", BooleanType(), True),
        StructField("file_url", StringType(), True),
        StructField("processed", LongType(), True),  # yyyyMMddHHmm of the run that chunked the paper
        StructField("embedding", ArrayType(FloatType()), True),
    ]
)
