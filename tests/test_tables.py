"""Tests for the incremental MERGE statements that update the bronze and silver tables."""

from consultation_paper_curator.schemas import SILVER_METADATA_SCHEMA, SOURCE_SCHEMA
from consultation_paper_curator.tables import bronze_merge_sql, silver_merge_sql

SOURCE_COLUMNS = SOURCE_SCHEMA.fieldNames()
SILVER_COLUMNS = SILVER_METADATA_SCHEMA.fieldNames()


def _clause(sql: str, starts_with: str) -> str:
    """Return the WHEN clause of a MERGE that starts with the given text."""
    return next(clause for clause in sql.split("WHEN ") if clause.startswith(starts_with))


def test_bronze_merge_ignores_a_new_run_timestamp_alone() -> None:
    """Re-scraping an unchanged document must not rewrite it, so first_seen_at and history stay intact."""
    matched = _clause(bronze_merge_sql("bronze", "scraped", SOURCE_COLUMNS), "MATCHED AND")
    condition = matched.split(" THEN")[0]

    assert "existing.ingestion_timestamp <=>" not in condition
    assert "NOT (existing.title <=> incoming.title)" in condition
    assert "NOT existing.is_listed" in condition


def test_bronze_merge_inserts_new_documents_as_listed_and_first_seen_now() -> None:
    """A new document records the current run as first_seen_at and is marked listed."""
    inserted = _clause(bronze_merge_sql("bronze", "scraped", SOURCE_COLUMNS), "NOT MATCHED THEN")

    assert inserted.rstrip().endswith("incoming.ingestion_timestamp, true)")


def test_bronze_merge_flags_documents_the_source_stopped_listing() -> None:
    """Documents missing from a scrape are kept and flagged, not removed."""
    missing = _clause(bronze_merge_sql("bronze", "scraped", SOURCE_COLUMNS), "NOT MATCHED BY SOURCE")

    assert "UPDATE SET existing.is_listed = false" in missing


def test_silver_merge_never_overwrites_download_storage() -> None:
    """Re-ingesting keeps processed/volume_path set by the download job; new papers start with none."""
    sql = silver_merge_sql("silver", "curated", SILVER_COLUMNS)
    updated = _clause(sql, "MATCHED AND")
    inserted = _clause(sql, "NOT MATCHED THEN")

    assert "processed" not in updated and "volume_path" not in updated
    assert inserted.rstrip().endswith("NULL, NULL)")
