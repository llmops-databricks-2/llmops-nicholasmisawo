"""Tests that the medallion table schemas match the rows the pipeline produces."""

from consultation_paper_curator.curation import curate_document
from consultation_paper_curator.schemas import (
    BRONZE_SCHEMA,
    SILVER_METADATA_SCHEMA,
    SILVER_SCHEMA,
    SOURCE_SCHEMA,
)
from consultation_paper_curator.sources.bma import parse_cards

SOURCE_ADDED_FIELDS = {"categories", "topics", "is_archived", "institution", "source", "ingestion_timestamp"}


def test_source_schema_covers_parsed_card_and_source_fields(bma_cards_html: str) -> None:
    """Every source column comes from a parsed card or is added by fetch_bma_documents."""
    [card] = parse_cards(bma_cards_html)

    assert set(SOURCE_SCHEMA.fieldNames()) == set(card) | SOURCE_ADDED_FIELDS


def test_bronze_schema_is_source_plus_tracking_columns() -> None:
    """Bronze keeps every source column and adds the MERGE-maintained history columns."""
    assert BRONZE_SCHEMA.fieldNames() == [*SOURCE_SCHEMA.fieldNames(), "first_seen_at", "is_listed"]


def test_silver_metadata_schema_matches_curated_rows(bma_cards_html: str) -> None:
    """The columns ingest writes to silver are exactly a curated row's fields, in order."""
    [card] = parse_cards(bma_cards_html)
    row = curate_document({**card, "source": "bma", "first_seen_at": "2026-10-23T00:00:00+00:00", "is_listed": True})

    assert SILVER_METADATA_SCHEMA.fieldNames() == list(row)


def test_silver_schema_adds_storage_columns_owned_by_download() -> None:
    """Silver is the curated metadata plus processed and volume_path, set by the download job."""
    assert SILVER_SCHEMA.fieldNames() == [*SILVER_METADATA_SCHEMA.fieldNames(), "processed", "volume_path"]
