"""Create the medallion Delta tables and build the MERGE statements that update them incrementally."""

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType

from consultation_paper_curator.schemas import STORAGE_FIELDS

KEY = "document_id"
# Changes on every run, so comparing it would rewrite every row
RUN_TIMESTAMP = "ingestion_timestamp"


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


def bronze_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """Upsert scraped rows into bronze, keeping first_seen_at and flagging documents no longer listed.

    Only rows whose content changed (or that reappear after being delisted) are updated.
    """
    compared = [column for column in columns if column not in (KEY, RUN_TIMESTAMP)]
    insert_columns = ", ".join([*columns, "first_seen_at", "is_listed"])
    insert_values = ", ".join([*(f"incoming.{column}" for column in columns), f"incoming.{RUN_TIMESTAMP}", "true"])

    return f"""
        MERGE INTO {target} AS existing
        USING {source_view} AS incoming
        ON existing.{KEY} = incoming.{KEY}
        WHEN MATCHED AND ({_any_changed(compared)} OR NOT existing.is_listed) THEN
            UPDATE SET {_assign(columns)}, existing.is_listed = true
        WHEN NOT MATCHED THEN
            INSERT ({insert_columns}) VALUES ({insert_values})
        WHEN NOT MATCHED BY SOURCE AND existing.is_listed THEN
            UPDATE SET existing.is_listed = false
    """


def silver_merge_sql(target: str, source_view: str, columns: list[str]) -> str:
    """Upsert curated rows into silver without touching processed/volume_path.

    New papers are inserted with no storage yet; papers that no longer survive curation (filtered out or
    superseded by a duplicate) are removed. Their files, if downloaded, stay in the volume.
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
