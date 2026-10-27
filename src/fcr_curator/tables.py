"""Create the Delta tables and build the MERGE statements that update them.

Every table is updated with MERGE on document_id rather than overwritten, so re-running a stage is
safe and only new or changed rows are written.
"""

from collections.abc import Iterable

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from fcr_curator.schemas import STORAGE_FIELDS

# Tables the Python pipeline lands, which dbt reads as sources (dbt/models/staging/_sources.yml)
SEARCH_RESULTS_TABLE = "raw_fcr_search_results"  # Every FCR PDF found by search or on a disclosures page
SEARCH_LOG_TABLE = "raw_fcr_search_log"  # One row per SerpAPI search made
REGISTER_TABLE = "raw_fcr_register"  # Curated FCRs, one per company and year, with their downloads
PARSED_TABLE = "raw_fcr_parsed_documents"  # ai_parse_document output per downloaded FCR
# Built by dbt (models/context/ai/fcr_knowledge_base.sql) and synced to Lakebase for search
KNOWLEDGE_BASE_TABLE = "fcr_knowledge_base"

KEY = "document_id"
# Ignored when deciding if a search-results row changed: the run time changes every run, and Google reorders results
VOLATILE_COLUMNS = ("ingestion_timestamp", "search_rank")
# The search results keep every query that ever found a row, not just this run's
ACCUMULATED_COLUMNS = ("search_queries",)


def ensure_table(spark: SparkSession, name: str, schema: StructType, layer: str, comment: str) -> None:
    """Create the table if it's missing, then set its description and layer."""
    if not spark.catalog.tableExists(name):
        spark.createDataFrame([], schema).write.format("delta").saveAsTable(name)
    escaped_comment = comment.replace("'", "\\'")
    spark.sql(f"COMMENT ON TABLE {name} IS '{escaped_comment}'")
    spark.sql(f"ALTER TABLE {name} SET TBLPROPERTIES ('quality' = '{layer}')")


def add_missing_columns(spark: SparkSession, name: str, schema: StructType) -> list[str]:
    """Add to the table any column of schema it doesn't have yet (Delta appends them); return their names.

    ensure_table creates a table from its schema only once, so columns added to the schema later need this.
    """
    existing = set(spark.table(name).columns)
    missing = [field for field in schema.fields if field.name not in existing]
    if missing:
        columns = ", ".join(f"{field.name} {field.dataType.simpleString()}" for field in missing)
        spark.sql(f"ALTER TABLE {name} ADD COLUMNS ({columns})")
    return [field.name for field in missing]


def column_moves(current: list[str], wanted: list[str]) -> list[tuple[str, str | None]]:
    """Return the moves that put current's columns in wanted's order: (column, column to follow or None for first).

    Columns not in wanted keep their place after the others. No moves if the order is already right.
    """
    ordered = [column for column in wanted if column in current]
    if [column for column in current if column in ordered] == ordered:
        return []
    return [(column, ordered[index - 1] if index else None) for index, column in enumerate(ordered)]


def align_column_order(spark: SparkSession, name: str, schema: StructType) -> int:
    """Put the table's columns in its schema's order; return how many moves that took.

    Columns added to a schema later land at the end of an existing table (see add_missing_columns), so
    this keeps the table reading the way the schema does. Moving a Delta column only changes metadata.
    """
    moves = column_moves(spark.table(name).columns, schema.fieldNames())
    for column, after in moves:
        spark.sql(f"ALTER TABLE {name} ALTER COLUMN {column} {'FIRST' if after is None else f'AFTER {after}'}")
    return len(moves)


def _changed(column: str, new_value: str) -> str:
    # <=> treats two NULLs as equal, so a change to or from NULL still counts
    return f"NOT (existing.{column} <=> {new_value})"


def _queries_so_far(column: str) -> str:
    # The row's existing queries plus this run's, sorted and without repeats
    return f"array_sort(array_union(coalesce(existing.{column}, array()), incoming.{column}))"


# --- Search results ---------------------------------------------------------------------------


def search_results_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """MERGE this run's search results into the search-results table.

    - New URL: inserted, with first_seen_at = this run and is_listed = true.
    - Known URL: updated only if something real changed (not just its rank or run time), a new query
      found it, or it was delisted and is back.
    - URLs this run didn't return are left alone; see delisted_ids.
    """
    replaced = [c for c in columns if c != KEY and c not in ACCUMULATED_COLUMNS]
    compared = [c for c in replaced if c not in VOLATILE_COLUMNS]
    accumulated = [c for c in columns if c in ACCUMULATED_COLUMNS]

    something_changed = " OR ".join(
        [
            *(_changed(c, f"incoming.{c}") for c in compared),
            *(_changed(c, _queries_so_far(c)) for c in accumulated),
            "NOT existing.is_listed",
        ]
    )
    updates = ", ".join(
        [
            *(f"existing.{c} = incoming.{c}" for c in replaced),
            *(f"existing.{c} = {_queries_so_far(c)}" for c in accumulated),
            "existing.is_listed = true",
        ]
    )
    insert_columns = ", ".join([*columns, "first_seen_at", "is_listed"])
    insert_values = ", ".join([*(f"incoming.{c}" for c in columns), "incoming.ingestion_timestamp", "true"])

    return f"""
        MERGE INTO {target} AS existing
        USING {source_view} AS incoming
        ON existing.{KEY} = incoming.{KEY}
        WHEN MATCHED AND ({something_changed}) THEN UPDATE SET {updates}
        WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})
    """


def delisted_ids(listed_rows: Iterable[dict], asked_queries: Iterable[str], returned_ids: Iterable[str]) -> list[str]:
    """Search-results rows to mark is_listed = false after this run.

    Only when every query that ever found the row was asked again this run and none of them returned
    it. A query the run didn't ask tells us nothing about the rows it found before.
    """
    asked, returned = set(asked_queries), set(returned_ids)
    return [
        row[KEY]
        for row in listed_rows
        if row[KEY] not in returned and row["search_queries"] and set(row["search_queries"]) <= asked
    ]


def delist_sql(target: str, ids_view: str) -> str:
    """Set is_listed = false on the rows whose document_id is in ids_view."""
    return f"""
        MERGE INTO {target} AS existing
        USING {ids_view} AS delisted
        ON existing.{KEY} = delisted.{KEY}
        WHEN MATCHED THEN UPDATE SET existing.is_listed = false
    """


# --- The FCR register -------------------------------------------------------------------------


def register_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """MERGE the curated FCRs into the register.

    - New FCR: inserted with processed and volume_path empty, until it's downloaded.
    - Known FCR: updated if its metadata changed; processed and volume_path are never touched.
    - FCR no longer curated (filtered out or a duplicate): deleted. Its file stays in the volume.
    """
    storage = [field.name for field in STORAGE_FIELDS]
    something_changed = " OR ".join(_changed(c, f"incoming.{c}") for c in columns if c != KEY)
    updates = ", ".join(f"existing.{c} = incoming.{c}" for c in columns)
    insert_columns = ", ".join([*columns, *storage])
    insert_values = ", ".join([*(f"incoming.{c}" for c in columns), *("NULL" for _ in storage)])

    return f"""
        MERGE INTO {target} AS existing
        USING {source_view} AS incoming
        ON existing.{KEY} = incoming.{KEY}
        WHEN MATCHED AND ({something_changed}) THEN UPDATE SET {updates}
        WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})
        WHEN NOT MATCHED BY SOURCE THEN DELETE
    """


def record_downloads_sql(target: str, downloads_view: str) -> str:
    """Set processed and volume_path on the register rows whose files were just downloaded."""
    return f"""
        MERGE INTO {target} AS existing
        USING {downloads_view} AS downloaded
        ON existing.{KEY} = downloaded.{KEY}
        WHEN MATCHED THEN UPDATE SET
            existing.processed = downloaded.processed,
            existing.volume_path = downloaded.volume_path
    """
