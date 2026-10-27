"""Land what the Python tasks observe as append-only facts.

The search and download tasks never update a row: each run appends what it saw, stamped with the
run's landed_at, and dbt works out the current state from all the runs. dbt also owns the tables'
columns (dbt/models/staging/_sources.yml) and creates the tables before every run, so a row is
appended in the table's own types, and a row whose columns don't match fails the task instead of
landing as nulls.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from pyspark.sql import SparkSession
from pyspark.sql.functions import lit
from pyspark.sql.types import StructType

LANDED_AT = "landed_at"  # When the run appended the row; one value per run, so it orders the runs


def check_row_columns(table: str, rows: Iterable[dict], columns: Iterable[str]) -> None:
    """Fail if any row has a column the table doesn't, or lacks one it does."""
    expected = set(columns)
    for row in rows:
        missing, unknown = expected - set(row), set(row) - expected
        if missing or unknown:
            raise ValueError(
                f"A row for {table} doesn't match its columns: missing {sorted(missing)}, unknown {sorted(unknown)}"
            )


def append_facts(spark: SparkSession, schema_name: str, tables: dict[str, list[dict]]) -> None:
    """Append each table's rows, all stamped with one landed_at for the run."""
    landed_at = datetime.now(UTC)
    for table, rows in tables.items():
        name = f"{schema_name}.{table}"
        if not spark.catalog.tableExists(name):
            raise RuntimeError(f"{name} not found; dbt creates it before every run, so run a dbt task first")
        if not rows:
            continue
        fact_schema = StructType([field for field in spark.table(name).schema.fields if field.name != LANDED_AT])
        check_row_columns(table, rows, fact_schema.fieldNames())
        facts = spark.createDataFrame(rows, schema=fact_schema).withColumn(LANDED_AT, lit(landed_at))
        facts.write.format("delta").mode("append").saveAsTable(name)
