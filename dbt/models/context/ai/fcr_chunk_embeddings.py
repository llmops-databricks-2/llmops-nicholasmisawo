"""Embed each chunk's text with the course's embedding service, course_ops.gateway.embed.

Python, not the package's SQL embed(): that calls ai_query, which can't reach a model service the
workspace created, so this calls the Unity Gateway API through the pipeline's own client
(fcr_curator.gateway), as the processing task does.

Each text is embedded once. A chunk whose id and content_hash already have a vector, here or in the
Python pipeline's table, reuses it; only new or changed text goes to the gateway.
"""

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.functions import col, sha2

EMBEDDING_SCHEMA = "id string, content_hash string, embedding array<float>"


def _reusable_vectors(dbt: object, session: SparkSession) -> DataFrame:
    """Vectors already made: this model's own, and the Python pipeline's for the same text."""
    pipeline = (
        dbt.source("landed", "gold_fcr_chunks_by_section")
        .where(col("embedding").isNotNull())
        .select("id", sha2(col("text"), 256).alias("content_hash"), "embedding")
    )
    if not dbt.is_incremental:
        return pipeline
    return session.table(str(dbt.this)).select("id", "content_hash", "embedding").unionByName(pipeline)


def model(dbt: object, session: SparkSession) -> DataFrame:
    """Return a vector for every chunk not yet embedded under its current content_hash."""
    from databricks.sdk import WorkspaceClient

    from fcr_curator.gateway import embed_texts, gateway_client, gateway_embedder

    dbt.config(materialized="incremental", incremental_strategy="merge", unique_key="id")
    chunks = dbt.ref("fcr_chunks_meta").select("id", "content_hash", "chunk_text")
    if dbt.is_incremental:  # Skip chunks whose current text this table already holds
        chunks = chunks.join(
            session.table(str(dbt.this)).select("id", "content_hash"), ["id", "content_hash"], "left_anti"
        )

    reusable = _reusable_vectors(dbt, session).dropDuplicates(["id", "content_hash"])
    reused = chunks.join(reusable, ["id", "content_hash"]).select("id", "content_hash", "embedding")
    missing = chunks.join(reusable, ["id", "content_hash"], "left_anti").collect()

    endpoint = dbt.config.get("embedding_endpoint")
    if not endpoint:
        raise ValueError("fcr_chunk_embeddings: set the embedding_endpoint config (dbt var embedding_endpoint)")
    embed = gateway_embedder(gateway_client(WorkspaceClient()), endpoint)
    vectors = embed_texts([row.chunk_text for row in missing], embed) if missing else []
    embedded = session.createDataFrame(
        [(row.id, row.content_hash, vector) for row, vector in zip(missing, vectors, strict=True)], EMBEDDING_SCHEMA
    )
    return reused.unionByName(embedded)
