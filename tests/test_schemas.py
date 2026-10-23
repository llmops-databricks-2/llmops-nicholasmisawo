"""Tests that the table schemas match the rows the pipeline produces."""

from consultation_paper_curator.curation import curate_document
from consultation_paper_curator.download import with_storage
from consultation_paper_curator.schemas import CURATED_SCHEMA, RAW_SCHEMA
from consultation_paper_curator.sources.bma import parse_cards

SOURCE_ADDED_FIELDS = {"categories", "topics", "is_archived", "institution", "source", "ingestion_timestamp"}


def test_raw_schema_covers_parsed_card_and_source_fields(bma_cards_html: str) -> None:
    """Every raw column comes from a parsed card or is added by fetch_bma_documents."""
    [card] = parse_cards(bma_cards_html)

    assert set(RAW_SCHEMA.fieldNames()) == set(card) | SOURCE_ADDED_FIELDS


def test_curated_schema_matches_curated_rows_with_storage(bma_cards_html: str) -> None:
    """The curated table's columns are exactly a curated row plus its volume location."""
    [card] = parse_cards(bma_cards_html)
    [row] = with_storage([curate_document({**card, "source": "bma"})], {})

    assert CURATED_SCHEMA.fieldNames() == list(row)
