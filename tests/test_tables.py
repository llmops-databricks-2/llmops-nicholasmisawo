"""Tests for the incremental MERGE statements that update the bronze and silver tables."""

from fcr_curator.schemas import REGISTER_METADATA_SCHEMA, SOURCE_SCHEMA
from fcr_curator.tables import (
    column_moves,
    delisted_ids,
    record_downloads_sql,
    register_merge_sql,
    search_results_merge_sql,
)

SOURCE_COLUMNS = SOURCE_SCHEMA.fieldNames()
SILVER_COLUMNS = REGISTER_METADATA_SCHEMA.fieldNames()


def _clause(sql: str, starts_with: str) -> str:
    """Return the WHEN clause of a MERGE that starts with the given text."""
    return next(clause for clause in sql.split("WHEN ") if clause.startswith(starts_with))


def test_bronze_merge_ignores_a_new_run_timestamp_alone() -> None:
    """Re-searching an unchanged document must not rewrite it, so first_seen_at and history stay intact."""
    matched = _clause(search_results_merge_sql("bronze", "scraped", SOURCE_COLUMNS), "MATCHED AND")
    condition = matched.split(" THEN")[0]

    assert "existing.ingestion_timestamp <=>" not in condition
    assert "NOT (existing.title <=> incoming.title)" in condition
    assert "NOT existing.is_listed" in condition


def test_bronze_merge_ignores_a_new_search_rank_alone() -> None:
    """Google reorders results between runs; that alone must not rewrite a bronze row."""
    matched = _clause(search_results_merge_sql("bronze", "search_results", SOURCE_COLUMNS), "MATCHED AND")
    condition = matched.split(" THEN")[0]

    assert "existing.search_rank <=>" not in condition
    assert "NOT (existing.search_queries <=> array_sort(array_union(" in condition


def test_bronze_merge_inserts_new_documents_as_listed_and_first_seen_now() -> None:
    """A new document records the current run as first_seen_at and is marked listed."""
    inserted = _clause(search_results_merge_sql("bronze", "scraped", SOURCE_COLUMNS), "NOT MATCHED THEN")

    assert inserted.rstrip().endswith("incoming.ingestion_timestamp, true)")


def test_bronze_merge_keeps_every_query_that_ever_found_a_row() -> None:
    """A run asks only some queries, so a matched row's queries are combined with the run's, not replaced."""
    matched = _clause(search_results_merge_sql("bronze", "search_results", SOURCE_COLUMNS), "MATCHED AND")

    assert "existing.search_queries = array_sort(array_union(" in matched
    assert "existing.search_queries = incoming.search_queries" not in matched


def test_bronze_merge_leaves_rows_the_run_did_not_return_alone() -> None:
    """Delisting is decided by delisted_ids, since a query the run didn't ask says nothing about its rows."""
    assert "NOT MATCHED BY SOURCE" not in search_results_merge_sql("bronze", "search_results", SOURCE_COLUMNS)


def test_delisted_ids_needs_every_query_of_a_row_asked_again_without_it() -> None:
    """Only a row all of whose queries were re-asked, and that none of them returned, is delisted."""
    listed = [
        {"document_id": "gone", "search_queries": ["a"]},
        {"document_id": "returned", "search_queries": ["a"]},
        {"document_id": "query_not_asked", "search_queries": ["a", "b"]},
    ]

    assert delisted_ids(listed, asked_queries={"a"}, returned_ids={"returned"}) == ["gone"]


def test_register_merge_never_overwrites_download_storage() -> None:
    """Re-ingesting keeps processed/volume_path set by the download job; new documents start with none."""
    sql = register_merge_sql("silver", "curated", SILVER_COLUMNS)
    updated = _clause(sql, "MATCHED AND")
    inserted = _clause(sql, "NOT MATCHED THEN")

    assert "processed" not in updated and "volume_path" not in updated
    assert inserted.rstrip().endswith("NULL, NULL)")


def test_record_downloads_sets_only_the_storage_columns() -> None:
    """Recording downloads touches processed and volume_path and nothing else."""
    updated = _clause(record_downloads_sql("silver", "downloads"), "MATCHED THEN")

    assert "existing.processed = downloaded.processed" in updated
    assert "existing.volume_path = downloaded.volume_path" in updated
    assert updated.count(" = ") == 2


def apply_moves(columns: list[str], moves: list[tuple[str, str | None]]) -> list[str]:
    """Return columns after each move, as ALTER COLUMN ... FIRST / AFTER would leave them."""
    result = list(columns)
    for column, after in moves:
        result.remove(column)
        result.insert(0 if after is None else result.index(after) + 1, column)
    return result


def test_column_moves_put_a_late_added_column_where_the_schema_wants_it() -> None:
    """A column appended at the end of a table moves to its place in the schema, here just after source."""
    current = ["document_id", "source", "institution", "volume_path", "registered_name"]
    wanted = ["document_id", "source", "registered_name", "institution", "volume_path"]

    assert apply_moves(current, column_moves(current, wanted)) == wanted


def test_column_moves_are_none_when_the_order_is_already_right() -> None:
    """A table already in schema order is left alone."""
    columns = ["document_id", "source", "registered_name"]

    assert column_moves(columns, columns) == []


def test_column_moves_leave_columns_outside_the_schema_after_the_others() -> None:
    """A table column the schema doesn't name isn't dropped or moved into the middle."""
    current = ["b", "a", "extra"]

    assert apply_moves(current, column_moves(current, ["a", "b"])) == ["a", "b", "extra"]
