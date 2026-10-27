"""Split each parsed FCR into section chunks with the pipeline's own chunker (fcr_curator.chunking).

Python, not the package's SQL chunk(): section_chunks follows the FCR's headings, starts every chunk
with its section heading and splits tables by rows, which measured far better on FCRs than packing
units by token count (docs/02-chunking.md). Chunking is cheap and deterministic, so the table is
rebuilt each run; the same text always gets the same id and content_hash, so its embedding is reused.
"""

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import col, concat_ws, explode, sha2, udf

CHUNK_TYPE = "array<struct<chunk_id:string,page_id:int,text:string>>"


def model(dbt: object, session: SparkSession) -> DataFrame:
    """Return one row per section chunk of every parsed FCR."""
    from fcr_curator.chunking import section_chunks  # Installed from the pipeline's wheel

    dbt.config(materialized="table")
    chunker = udf(section_chunks, CHUNK_TYPE)
    return (
        dbt.ref("fcr_parsed_hashed")
        .withColumn("chunk", explode(chunker(col("parsed_content"))))
        .select(
            concat_ws("_", col("document_id"), col("chunk.chunk_id")).alias("id"),  # As the pipeline names chunks
            col("document_id"),
            col("chunk.chunk_id").cast("int").alias("chunk_seq"),
            col("chunk.page_id").alias("page_id"),
            col("chunk.text").alias("chunk_text"),
            sha2(col("chunk.text"), 256).alias("content_hash"),
        )
    )
