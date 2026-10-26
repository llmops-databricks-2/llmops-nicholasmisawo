"""Tests that the medallion table schemas match the rows the pipeline produces."""

from fcr_curator.curation import curate_document
from fcr_curator.schemas import (
    BRONZE_SCHEMA,
    CHUNKS_SCHEMA,
    EMBEDDED_CHUNKS_SCHEMA,
    SILVER_METADATA_SCHEMA,
    SILVER_SCHEMA,
    SOURCE_SCHEMA,
)
from fcr_curator.sources.serpapi import merge_rows, parse_results

SOURCE_ADDED_FIELDS = {"source", "ingestion_timestamp"}


def _source_row(serpapi_page: dict) -> dict:
    [row, *_] = merge_rows(parse_results(serpapi_page["organic_results"], "q"))
    return row


def test_source_schema_covers_parsed_result_and_source_fields(serpapi_page: dict) -> None:
    """Every source column comes from a parsed result or is added by fetch_fcr_documents."""
    assert set(SOURCE_SCHEMA.fieldNames()) == set(_source_row(serpapi_page)) | SOURCE_ADDED_FIELDS


def test_bronze_schema_is_source_plus_tracking_columns() -> None:
    """Bronze keeps every source column and adds the MERGE-maintained history columns."""
    assert BRONZE_SCHEMA.fieldNames() == [*SOURCE_SCHEMA.fieldNames(), "first_seen_at", "is_listed"]


def test_silver_metadata_schema_matches_curated_rows(serpapi_page: dict) -> None:
    """The columns ingest writes to silver are exactly a curated row's fields, in order."""
    bronze_row = {
        **_source_row(serpapi_page),
        "source": "serpapi",
        "first_seen_at": "2026-10-25T00:00:00+00:00",
        "is_listed": True,
    }

    assert SILVER_METADATA_SCHEMA.fieldNames() == list(curate_document(bronze_row))


def test_silver_schema_adds_storage_columns_owned_by_download() -> None:
    """Silver is the curated metadata plus processed and volume_path, set by the download job."""
    assert SILVER_SCHEMA.fieldNames() == [*SILVER_METADATA_SCHEMA.fieldNames(), "processed", "volume_path"]


def test_embedded_chunks_schema_is_the_chunks_schema_plus_an_embedding() -> None:
    """The searchable chunks table adds only the embedding column, so it syncs the same metadata."""
    assert EMBEDDED_CHUNKS_SCHEMA.fieldNames() == [*CHUNKS_SCHEMA.fieldNames(), "embedding"]
