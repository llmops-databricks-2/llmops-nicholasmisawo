"""Column layout of the FCR register, which dbt's int_fcr_register Python model builds with curation.py.

The tables the Python tasks land are defined in dbt (dbt/models/staging/_sources.yml); see landing.py.
"""

from pyspark.sql.types import ArrayType, BooleanType, IntegerType, StringType, StructField, StructType

# Curated metadata (see curation.curate), rebuilt from the search results on every run
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

# int_fcr_register: curated FCRs plus where each one's PDF goes in the volume (see download.document_path)
REGISTER_SCHEMA = StructType([*REGISTER_METADATA_SCHEMA.fields, StructField("volume_path", StringType(), False)])

# raw_fcr_download_attempts: one row per attempt to fetch an FCR's PDF (download.DownloadOutcome)
DOWNLOAD_ATTEMPT_SCHEMA = StructType(
    [
        StructField("document_id", StringType(), False),
        StructField("file_url", StringType(), True),
        StructField("outcome", StringType(), False),  # downloaded, failed or rejected
        StructField("attempted_at", StringType(), False),  # UTC ISO
    ]
)
