"""Embed each chunk's text with the course's embedding service, course_ops.gateway.embed.

Python, not the package's SQL embed(): that calls ai_query, which can't reach a model service the
workspace created, so this calls the Unity Gateway API through the pipeline's own client
(fcr_curator.gateway).

Incremental, and each text is embedded once: only chunks with no vector here yet for their id and
content_hash go to the gateway.
"""

from pyspark.sql import DataFrame, SparkSession

EMBEDDING_SCHEMA = "id string, content_hash string, embedding array<float>"


def model(dbt: object, session: SparkSession) -> DataFrame:
    """Return a vector for every chunk not yet embedded under its current content_hash."""
    from databricks.sdk import WorkspaceClient

    from fcr_curator.gateway import embed_texts, gateway_client, gateway_embedder

    dbt.config(materialized="incremental", incremental_strategy="merge", unique_key="id")
    chunks = dbt.ref("fcr_chunks_meta").select("id", "content_hash", "chunk_text")
    if dbt.is_incremental:  # Skip chunks whose current text this table already holds
        embedded = session.table(str(dbt.this)).select("id", "content_hash")
        chunks = chunks.join(embedded, ["id", "content_hash"], "left_anti")
    missing = chunks.collect()

    endpoint = dbt.config.get("embedding_endpoint")
    if not endpoint:
        raise ValueError("fcr_chunk_embeddings: set the embedding_endpoint config (dbt var embedding_endpoint)")
    embed = gateway_embedder(gateway_client(WorkspaceClient()), endpoint)
    vectors = embed_texts([row.chunk_text for row in missing], embed) if missing else []
    return session.createDataFrame(
        [(row.id, row.content_hash, vector) for row, vector in zip(missing, vectors, strict=True)], EMBEDDING_SCHEMA
    )
