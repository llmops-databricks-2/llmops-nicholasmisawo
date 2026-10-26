"""Create the Delta tables and build the MERGE statements that update them.

Every table is updated with MERGE on document_id rather than overwritten, so re-running a stage is
safe and only new or changed rows are written.
"""

from collections.abc import Iterable

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from fcr_curator.schemas import STORAGE_FIELDS

KEY = "document_id"
# Ignored when deciding if a bronze row changed: the run time changes every run, and Google reorders results
VOLATILE_COLUMNS = ("ingestion_timestamp", "search_rank")
# Bronze keeps every query that ever found a row, not just this run's
ACCUMULATED_COLUMNS = ("search_queries",)


def ensure_table(spark: SparkSession, name: str, schema: StructType, layer: str, comment: str) -> None:
    """Create the table if it's missing, then set its description and medallion layer."""
    if not spark.catalog.tableExists(name):
        spark.createDataFrame([], schema).write.format("delta").saveAsTable(name)
    escaped_comment = comment.replace("'", "\\'")
    spark.sql(f"COMMENT ON TABLE {name} IS '{escaped_comment}'")
    spark.sql(f"ALTER TABLE {name} SET TBLPROPERTIES ('quality' = '{layer}')")


def _changed(column: str, new_value: str) -> str:
    # <=> treats two NULLs as equal, so a change to or from NULL still counts
    return f"NOT (existing.{column} <=> {new_value})"


def _queries_so_far(column: str) -> str:
    # The row's existing queries plus this run's, sorted and without repeats
    return f"array_sort(array_union(coalesce(existing.{column}, array()), incoming.{column}))"


# --- Bronze -----------------------------------------------------------------------------------


def bronze_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """MERGE this run's search results into bronze.

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
    """Bronze rows to mark is_listed = false after this run.

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


# --- Silver -----------------------------------------------------------------------------------


def silver_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """MERGE the curated FCRs into silver.

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
    """Set processed and volume_path on the silver rows whose files were just downloaded."""
    return f"""
        MERGE INTO {target} AS existing
        USING {downloads_view} AS downloaded
        ON existing.{KEY} = downloaded.{KEY}
        WHEN MATCHED THEN UPDATE SET
            existing.processed = downloaded.processed,
            existing.volume_path = downloaded.volume_path
    """


# --- Gold -------------------------------------------------------------------------------------


def record_embeddings_sql(target: str, embeddings_view: str) -> str:
    """Set the embedding on the chunks whose id is in embeddings_view."""
    return f"""
        MERGE INTO {target} AS existing
        USING {embeddings_view} AS embedded
        ON existing.id = embedded.id
        WHEN MATCHED THEN UPDATE SET existing.embedding = embedded.embedding
    """
