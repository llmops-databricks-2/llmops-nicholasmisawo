"""Tests that the register's schema matches the rows curation produces."""

from fcr_curator.curation import curate_document
from fcr_curator.schemas import REGISTER_METADATA_SCHEMA, REGISTER_SCHEMA
from fcr_curator.sources.serpapi import merge_rows, parse_results


def _source_row(serpapi_page: dict) -> dict:
    [row, *_] = merge_rows(parse_results(serpapi_page["organic_results"], "q"))
    return row


def test_register_metadata_schema_matches_curated_rows(serpapi_page: dict) -> None:
    """The register's curated columns are exactly a curated row's fields, in order."""
    search_result = {
        **_source_row(serpapi_page),
        "source": "serpapi",
        "first_seen_at": "2026-10-25T00:00:00+00:00",
        "is_listed": True,
    }

    assert REGISTER_METADATA_SCHEMA.fieldNames() == list(curate_document(search_result))


def test_register_schema_adds_where_each_pdf_goes() -> None:
    """The register is the curated metadata plus the volume path the download task writes the PDF to."""
    assert REGISTER_SCHEMA.fieldNames() == [*REGISTER_METADATA_SCHEMA.fieldNames(), "volume_path"]
