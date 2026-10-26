"""Parse the downloaded FCR PDFs and split them into chunks for retrieval.

silver_fcr_documents          FCRs, with volume_path once downloaded
    | parse_pending_documents   ai_parse_document, once per file
silver_parsed_fcr_documents   the parser's output as JSON
    | chunk_parsed_documents    one gold table per chunking strategy (see chunking.py)
gold_fcr_chunks               strategy 1: one chunk per element
gold_fcr_chunks_by_section    strategy 2: one chunk per section, size-limited
    | embed_pending_chunks      embeddings, for the strategy kept for search only

Each FCR is parsed once and chunked once per strategy, and each chunk is embedded once, so a re-run
only handles FCRs downloaded since.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from loguru import logger
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import col, concat_ws, explode, expr, lit, regexp_replace, udf

from fcr_curator.chunking import Chunk, element_chunks, section_chunks
from fcr_curator.config import ProjectConfig
from fcr_curator.download import processed_stamp
from fcr_curator.gateway import EmbedBatch, embed_texts
from fcr_curator.schemas import CHUNKS_SCHEMA, EMBEDDED_CHUNKS_SCHEMA, PARSED_SCHEMA
from fcr_curator.tables import KEY, add_missing_columns, ensure_table, record_embeddings_sql, refresh_chunk_metadata_sql

# Turn off the model-written figure descriptions: they cost extra, and chunks only use text and tables
PARSE_OPTIONS = "map('version', '2.0', 'descriptionElementTypes', '')"
CHUNK_TYPE = "array<struct<chunk_id:string,page_id:int,text:string>>"

# Gold table name -> the chunking strategy that fills it
CHUNKING_STRATEGIES: dict[str, Callable[[str | None], list[Chunk]]] = {
    "gold_fcr_chunks": element_chunks,
    "gold_fcr_chunks_by_section": section_chunks,
}
# The strategy kept for search: its chunks are embedded and synced to Lakebase
SEARCH_CHUNKS_TABLE = "gold_fcr_chunks_by_section"
# Silver columns every chunk carries, kept current on each run
CHUNK_METADATA_COLUMNS = ["title", "institution", "registered_name", "domain", "reporting_year", "file_url"]
# Chunks embedded and saved per batch, so a failure keeps the batches already done
EMBED_SAVE_BATCH = 2000


# --- The Spark workflow -----------------------------------------------------------------------


class DataProcessor:
    """Runs parse -> chunk -> embed over silver, never repeating work.

    embed embeds a batch of texts (see gateway.gateway_embedder); None skips embedding.
    """

    def __init__(self, spark: SparkSession, config: ProjectConfig, embed: EmbedBatch | None = None) -> None:
        self.spark = spark
        self.cfg = config
        self.embed = embed
        self.documents_table = f"{config.full_schema_name}.silver_fcr_documents"
        self.parsed_table = f"{config.full_schema_name}.silver_parsed_fcr_documents"
        self.chunk_tables = {
            f"{config.full_schema_name}.{name}": chunker for name, chunker in CHUNKING_STRATEGIES.items()
        }
        self.search_table = f"{config.full_schema_name}.{SEARCH_CHUNKS_TABLE}"
        self.run_stamp = processed_stamp(datetime.now(UTC))

    def process_and_save(self) -> None:
        """Run every step in order."""
        self.ensure_tables()
        self.remove_dropped_documents()
        logger.info(f"Parsed {self.parse_pending_documents()} documents into {self.parsed_table}")
        for table, chunker in self.chunk_tables.items():
            self.refresh_chunk_metadata(table)
            logger.info(f"Wrote {self.chunk_parsed_documents(table, chunker)} chunks to {table}")
        if self.embed:
            logger.info(f"Embedded {self.embed_pending_chunks()} chunks in {self.search_table}")

    def ensure_tables(self) -> None:
        """Create the parsed table and one chunks table per strategy, if they're missing."""
        if not self.spark.catalog.tableExists(self.documents_table):
            raise RuntimeError(f"{self.documents_table} not found; run the silver_fcr_documents task first")

        ensure_table(
            self.spark,
            self.parsed_table,
            PARSED_SCHEMA,
            layer="silver",
            comment="Silver: ai_parse_document output for each downloaded FCR, parsed once.",
        )
        for table, chunker in self.chunk_tables.items():
            searched = table == self.search_table
            schema = EMBEDDED_CHUNKS_SCHEMA if searched else CHUNKS_SCHEMA
            comment = f"Gold: FCR chunks made by chunking.{chunker.__name__}, with document metadata" + (
                " and embeddings; synced to Lakebase for search." if searched else "; not embedded."
            )
            ensure_table(self.spark, table, schema, layer="gold", comment=comment)
            add_missing_columns(self.spark, table, schema)  # Columns added to the schema after the table was made
            # Change Data Feed lets a Lakebase synced table follow this table's changes
            self.spark.sql(f"ALTER TABLE {table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

    def refresh_chunk_metadata(self, table: str) -> None:
        """Bring the metadata on table's existing chunks up to date with silver."""
        self.spark.sql(refresh_chunk_metadata_sql(table, self.documents_table, CHUNK_METADATA_COLUMNS))

    def remove_dropped_documents(self) -> None:
        """Delete parsed rows and chunks of FCRs silver no longer has (filtered out or duplicates)."""
        for table in (self.parsed_table, *self.chunk_tables):
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

    def chunk_parsed_documents(self, table: str, chunker: Callable[[str | None], list[Chunk]]) -> int:
        """Chunk the parsed FCRs not yet in table with chunker, add their metadata; return the chunks written."""
        chunker_udf = udf(chunker, CHUNK_TYPE)
        already_chunked = self.spark.table(table).select(KEY).distinct()
        metadata = self.spark.table(self.documents_table).select(KEY, *CHUNK_METADATA_COLUMNS)

        chunks_df: DataFrame = (
            self.spark.table(self.parsed_table)
            .join(already_chunked, KEY, "left_anti")
            .withColumn("chunk", explode(chunker_udf(col("parsed_content"))))
            .select(KEY, "chunk.chunk_id", "chunk.page_id", "chunk.text")
            .withColumn("id", concat_ws("_", col(KEY), col("chunk_id")))  # Unique per chunk: <document_id>_<chunk_id>
            .withColumn("processed", lit(self.run_stamp).cast("long"))
            .join(metadata, KEY)
        )
        chunks_df.select(*CHUNKS_SCHEMA.fieldNames()).write.format("delta").mode("append").saveAsTable(table)
        # Count from the table: counting the DataFrame would run the chunking again
        return self.spark.table(table).where(col("processed") == self.run_stamp).count()

    def embed_pending_chunks(self) -> int:
        """Embed the search table's chunks that have no embedding yet; return how many were embedded."""
        pending = self.spark.table(self.search_table).where(col("embedding").isNull()).select("id", "text").collect()
        for start in range(0, len(pending), EMBED_SAVE_BATCH):
            batch = pending[start : start + EMBED_SAVE_BATCH]
            vectors = embed_texts([row.text for row in batch], self.embed)
            self.spark.createDataFrame(
                [(row.id, vector) for row, vector in zip(batch, vectors, strict=True)],
                "id string, embedding array<float>",
            ).createOrReplaceTempView("new_embeddings")
            self.spark.sql(record_embeddings_sql(self.search_table, "new_embeddings"))
            logger.info(f"Embedded {start + len(batch)} of {len(pending)} chunks")
        return len(pending)
