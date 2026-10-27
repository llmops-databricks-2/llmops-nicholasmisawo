"""Parse the downloaded FCR PDFs with ai_parse_document, once per file.

    raw_fcr_register            curated FCRs, with volume_path once downloaded
        | parse_pending_documents
    raw_fcr_parsed_documents    the parser's output as JSON, which dbt chunks and embeds

Chunking, embedding and the knowledge base are dbt models (dbt/models/context/).
"""

from datetime import UTC, datetime

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, expr, lit, regexp_replace

from fcr_curator.config import ProjectConfig
from fcr_curator.download import processed_stamp
from fcr_curator.schemas import PARSED_SCHEMA
from fcr_curator.tables import KEY, PARSED_TABLE, REGISTER_TABLE, ensure_table

# Turn off the model-written figure descriptions: they cost extra, and chunks only use text and tables
PARSE_OPTIONS = "map('version', '2.0', 'descriptionElementTypes', '')"


class DocumentParser:
    """Parses each downloaded FCR once, and drops the parses of FCRs the register no longer holds."""

    def __init__(self, spark: SparkSession, config: ProjectConfig) -> None:
        self.spark = spark
        self.register_table = f"{config.full_schema_name}.{REGISTER_TABLE}"
        self.parsed_table = f"{config.full_schema_name}.{PARSED_TABLE}"
        self.run_stamp = processed_stamp(datetime.now(UTC))

    def ensure_table(self) -> None:
        """Create the parsed table if it's missing."""
        if not self.spark.catalog.tableExists(self.register_table):
            raise RuntimeError(f"{self.register_table} not found; run the curate_and_download_fcrs task first")
        ensure_table(
            self.spark, self.parsed_table, PARSED_SCHEMA, "raw", "Raw: ai_parse_document output per downloaded FCR."
        )

    def remove_dropped_documents(self) -> None:
        """Delete parses of FCRs the register no longer holds (filtered out, or superseded by a duplicate)."""
        self.spark.sql(f"DELETE FROM {self.parsed_table} WHERE {KEY} NOT IN (SELECT {KEY} FROM {self.register_table})")

    def parse_pending_documents(self) -> int:
        """Parse downloaded FCRs that haven't been parsed yet; return how many."""
        downloaded = self.spark.table(self.register_table).where(col("volume_path").isNotNull())
        pending = downloaded.join(self.spark.table(self.parsed_table).select(KEY), KEY, "left_anti")
        paths = [row.volume_path for row in pending.select("volume_path").collect()]
        if not paths:
            return 0

        parsed_df = (
            self.spark.read.format("binaryFile")
            .load(paths)
            # binaryFile reports paths as dbfs:/Volumes/...; the register stores /Volumes/...
            .withColumn("volume_path", regexp_replace(col("path"), "^dbfs:", ""))
            .join(pending.select(KEY, "volume_path"), "volume_path")
            .withColumn("parsed", expr(f"ai_parse_document(content, {PARSE_OPTIONS})"))
            .select(
                col(KEY),
                col("volume_path"),
                expr("to_json(parsed)").alias("parsed_content"),
                # A missing error_status turns into the text 'null'; store a real NULL for "no errors"
                expr("nullif(nullif(to_json(parsed:error_status), 'null'), '[]')").alias("parse_errors"),
                lit(self.run_stamp).cast("long").alias("processed"),
            )
        )
        parsed_df.write.format("delta").mode("append").saveAsTable(self.parsed_table)
        return len(paths)
