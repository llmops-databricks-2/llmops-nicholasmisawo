"""Create the medallion Delta tables and build the MERGE statements that update them incrementally."""

from collections.abc import Iterable

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from fcr_curator.schemas import STORAGE_FIELDS

KEY = "document_id"
# Changes on every run, so comparing it would rewrite every row
RUN_TIMESTAMP = "ingestion_timestamp"
# Google reorders results between runs; a new rank alone isn't a change to the document
VOLATILE_COLUMNS = (RUN_TIMESTAMP, "search_rank")
# A run asks only some queries, so these keep every value any run found rather than the latest run's
ACCUMULATED_COLUMNS = ("search_queries",)


def ensure_table(spark: SparkSession, name: str, schema: StructType, layer: str, comment: str) -> None:
    """Create the Delta table if missing, then label it with its medallion layer and a description."""
    if not spark.catalog.tableExists(name):
        spark.createDataFrame([], schema).write.format("delta").saveAsTable(name)

    escaped_comment = comment.replace("'", "\\'")
    spark.sql(f"COMMENT ON TABLE {name} IS '{escaped_comment}'")
    spark.sql(f"ALTER TABLE {name} SET TBLPROPERTIES ('quality' = '{layer}')")


def _any_changed(columns: list[str]) -> str:
    # <=> is null-safe, so a value changing to or from NULL counts as a change
    return " OR ".join(f"NOT (existing.{column} <=> incoming.{column})" for column in columns)


def _assign(columns: list[str]) -> str:
    return ", ".join(f"existing.{column} = incoming.{column}" for column in columns)


def _accumulated(column: str) -> str:
    return f"array_sort(array_union(coalesce(existing.{column}, array()), incoming.{column}))"


def bronze_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """Upsert source rows into bronze, keeping first_seen_at and every query that ever found a row.

    Only rows whose content changed, that a new query found, or that reappear after being delisted
    are updated; a change in VOLATILE_COLUMNS alone leaves the row as it was. A run asks only some of
    the queries, so rows it didn't return aren't touched here: see delisted_ids.
    """
    accumulated = [column for column in columns if column in ACCUMULATED_COLUMNS]
    replaced = [column for column in columns if column != KEY and column not in ACCUMULATED_COLUMNS]
    compared = [column for column in replaced if column not in VOLATILE_COLUMNS]
    changed = " OR ".join(
        [_any_changed(compared), *(f"NOT (existing.{column} <=> {_accumulated(column)})" for column in accumulated)]
    )
    assignments = ", ".join(
        [_assign(replaced), *(f"existing.{column} = {_accumulated(column)}" for column in accumulated)]
    )
    insert_columns = ", ".join([*columns, "first_seen_at", "is_listed"])
    insert_values = ", ".join(
        [
            *(f"incoming.{column}" for column in columns),
            f"incoming.{RUN_TIMESTAMP}",
            "true",
        ]
    )

    return f"""
        MERGE INTO {target} AS existing
        USING {source_view} AS incoming
        ON existing.{KEY} = incoming.{KEY}
        WHEN MATCHED AND ({changed} OR NOT existing.is_listed) THEN
            UPDATE SET {assignments}, existing.is_listed = true
        WHEN NOT MATCHED THEN
            INSERT ({insert_columns}) VALUES ({insert_values})
    """


def delisted_ids(listed_rows: Iterable[dict], asked_queries: Iterable[str], returned_ids: Iterable[str]) -> list[str]:
    """Return the listed rows this run should mark as no longer found.

    A row is delisted only when every query that ever found it was asked again this run and none
    returned it; a query the run didn't reach says nothing about the rows it found before.
    """
    asked, returned = set(asked_queries), set(returned_ids)
    return [
        row[KEY]
        for row in listed_rows
        if row[KEY] not in returned and row["search_queries"] and set(row["search_queries"]) <= asked
    ]


def delist_sql(target: str, ids_view: str) -> str:
    """Mark the rows whose document_id is in ids_view as no longer listed."""
    return f"""
        MERGE INTO {target} AS existing
        USING {ids_view} AS delisted
        ON existing.{KEY} = delisted.{KEY}
        WHEN MATCHED THEN
            UPDATE SET existing.is_listed = false
    """


def silver_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """Upsert curated rows into silver without touching processed/volume_path.

    New documents are inserted with no storage yet; documents that no longer survive curation (filtered
    out or superseded by a duplicate) are removed. Their files, if downloaded, stay in the volume.
    """
    storage = [field.name for field in STORAGE_FIELDS]
    compared = [column for column in columns if column != KEY]
    insert_columns = ", ".join([*columns, *storage])
    insert_values = ", ".join([*(f"incoming.{column}" for column in columns), *("NULL" for _ in storage)])

    return f"""
        MERGE INTO {target} AS existing
        USING {source_view} AS incoming
        ON existing.{KEY} = incoming.{KEY}
        WHEN MATCHED AND ({_any_changed(compared)}) THEN
            UPDATE SET {_assign(columns)}
        WHEN NOT MATCHED THEN
            INSERT ({insert_columns}) VALUES ({insert_values})
        WHEN NOT MATCHED BY SOURCE THEN
            DELETE
    """
