"""Parse the downloaded FCR PDFs and split them into clean chunks for retrieval.

silver_fcr_documents          FCRs, with volume_path once downloaded
    | parse_pending_documents   ai_parse_document, once per file
silver_parsed_fcr_documents   the parser's output as JSON
    | chunk_parsed_documents    text and table elements -> cleaned chunks
gold_fcr_chunks               one row per chunk, with its FCR's metadata (not embedded yet)

Each FCR is parsed once and chunked once, so a re-run only handles FCRs downloaded since.
"""

import json
import re
from datetime import UTC, datetime
from typing import NamedTuple

from loguru import logger
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import col, concat_ws, explode, expr, lit, regexp_replace, udf

from fcr_curator.config import ProjectConfig
from fcr_curator.download import processed_stamp
from fcr_curator.schemas import CHUNKS_SCHEMA, PARSED_SCHEMA
from fcr_curator.tables import KEY, ensure_table

# Parsed element types that become chunks. Tables hold the solvency and capital figures, so they're kept;
# titles, headers, footers and page numbers are too short or repetitive to be useful on their own.
CHUNK_ELEMENT_TYPES = ("text", "table")
# Turn off the model-written figure descriptions: they cost extra, and chunks only use text and tables
PARSE_OPTIONS = "map('version', '2.0', 'descriptionElementTypes', '')"


class Chunk(NamedTuple):
    """One piece of an FCR."""

    chunk_id: str  # The parsed element's id
    page_id: int | None  # Page it starts on, for citations
    text: str


# --- Chunking rules (plain Python, so they can be tested without Spark) -----------------------


def clean_chunk(text: str) -> str:
    """Rejoin words split by a hyphen at a line break, and collapse all whitespace to single spaces."""
    joined = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text or "")  # "docu-\nments" -> "documents"
    return re.sub(r"\s+", " ", joined).strip()


def extract_chunks(parsed_content_json: str | None) -> list[Chunk]:
    """One chunk per non-empty text or table element of an ai_parse_document result."""
    if not parsed_content_json:
        return []

    chunks = []
    for element in json.loads(parsed_content_json).get("document", {}).get("elements", []):
        text = clean_chunk(element.get("content", ""))
        if element.get("type") in CHUNK_ELEMENT_TYPES and text:
            boxes = element.get("bbox") or []
            page_id = boxes[0].get("page_id") if boxes else None
            chunks.append(Chunk(str(element.get("id", "")), page_id, text))
    return chunks


# --- The Spark workflow -----------------------------------------------------------------------


class DataProcessor:
    """Runs parse -> chunk over silver, one FCR at a time, never repeating work."""

    def __init__(self, spark: SparkSession, config: ProjectConfig) -> None:
        self.spark = spark
        self.cfg = config
        self.documents_table = f"{config.full_schema_name}.silver_fcr_documents"
        self.parsed_table = f"{config.full_schema_name}.silver_parsed_fcr_documents"
        self.chunks_table = f"{config.full_schema_name}.gold_fcr_chunks"
        self.run_stamp = processed_stamp(datetime.now(UTC))

    def process_and_save(self) -> None:
        """Run every step in order."""
        self.ensure_tables()
        self.remove_dropped_documents()
        logger.info(f"Parsed {self.parse_pending_documents()} documents into {self.parsed_table}")
        logger.info(f"Wrote {self.chunk_parsed_documents()} chunks to {self.chunks_table}")

    def ensure_tables(self) -> None:
        """Create the parsed and chunks tables if they're missing."""
        if not self.spark.catalog.tableExists(self.documents_table):
            raise RuntimeError(f"{self.documents_table} not found; run the silver_fcr_documents task first")

        ensure_table(
            self.spark,
            self.parsed_table,
            PARSED_SCHEMA,
            layer="silver",
            comment="Silver: ai_parse_document output for each downloaded FCR, parsed once.",
        )
        ensure_table(
            self.spark,
            self.chunks_table,
            CHUNKS_SCHEMA,
            layer="gold",
            comment="Gold: cleaned text and table chunks of FCRs with document metadata, not embedded yet.",
        )
        # Change Data Feed lets a Vector Search index or Lakebase synced table follow this table's changes
        self.spark.sql(f"ALTER TABLE {self.chunks_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

    def remove_dropped_documents(self) -> None:
        """Delete parsed rows and chunks of FCRs silver no longer has (filtered out or duplicates)."""
        for table in (self.parsed_table, self.chunks_table):
            self.spark.sql(f"DELETE FROM {table} WHERE {KEY} NOT IN (SELECT {KEY} FROM {self.documents_table})")

    def parse_pending_documents(self) -> int:
        """Parse downloaded FCRs that haven't been parsed yet; return how many."""
        downloaded = self.spark.table(self.documents_table).where(col("volume_path").isNotNull())
        pending = downloaded.join(self.spark.table(self.parsed_table).select(KEY), KEY, "left_anti")
        paths = [row.volume_path for row in pending.select("volume_path").collect()]
        if not paths:
            return 0

        parsed_df = (
            self.spark.read.format("binaryFile")
            .load(paths)
            # binaryFile reports paths as dbfs:/Volumes/...; silver stores /Volumes/...
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

    def chunk_parsed_documents(self) -> int:
        """Chunk parsed FCRs that haven't been chunked yet, add their metadata; return the chunks written."""
        extract_chunks_udf = udf(extract_chunks, "array<struct<chunk_id:string,page_id:int,text:string>>")
        already_chunked = self.spark.table(self.chunks_table).select(KEY).distinct()
        metadata = self.spark.table(self.documents_table).select(
            KEY, "title", "institution", "domain", "reporting_year", "file_url"
        )

        chunks_df: DataFrame = (
            self.spark.table(self.parsed_table)
            .join(already_chunked, KEY, "left_anti")
            .withColumn("chunk", explode(extract_chunks_udf(col("parsed_content"))))
            .select(KEY, "chunk.chunk_id", "chunk.page_id", "chunk.text")
            .withColumn("id", concat_ws("_", col(KEY), col("chunk_id")))  # Unique per chunk: <document_id>_<chunk_id>
            .withColumn("processed", lit(self.run_stamp).cast("long"))
            .join(metadata, KEY)
        )
        chunks_df.select(*CHUNKS_SCHEMA.fieldNames()).write.format("delta").mode("append").saveAsTable(
            self.chunks_table
        )
        # Count from the table: counting the DataFrame would run the chunking again
        return self.spark.table(self.chunks_table).where(col("processed") == self.run_stamp).count()
