"""Spark schemas of the project's Delta tables, shared by the notebooks that write and read them."""

from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DateType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# Raw rows as each source returns them (see sources/)
RAW_SCHEMA = StructType(
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
        StructField("ingestion_timestamp", StringType(), True),
    ]
)

# Curated papers (see curation.curate), plus where each file landed in the volume
CURATED_SCHEMA = StructType(
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
        StructField("related_documents", ArrayType(StringType()), True),
        StructField("superseded_ids", ArrayType(StringType()), True),
        StructField("ingestion_timestamp", StringType(), True),
        StructField("downloaded_at", TimestampType(), True),
        StructField("volume_path", StringType(), True),
    ]
)
