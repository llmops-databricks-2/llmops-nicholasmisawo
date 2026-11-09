"""Search the Lakebase copy of the FCR knowledge base from a question, in one object.

LakebasePgVectorManager is the course's Lakebase pgvector manager, fitted to this project: it ties
together what lakebase.py, gateway.py and search.py already do, so a caller (the assistant's retrieval
tool, a notebook) needs only a question and, optionally, filters:

- the connection: lakebase.connect, a fresh OAuth token each time, since tokens last about an hour
- the query embedding: the same Unity Gateway model service that embedded the chunks
- the searches: search.ChunkSearch, returning SearchHits that reranking and the context budget take

It also sets up the synced table for search: pgvector (ensure_extension) and a full-text index
(ensure_indexes). The sync owns the table's rows; the index is the only thing added to it.

Run it from your machine, not a job: psycopg's compiled library aborts Python on serverless job compute.
"""

from databricks.sdk import WorkspaceClient
from loguru import logger
from psycopg import sql

from fcr_curator.config import ProjectConfig
from fcr_curator.gateway import EmbedBatch, gateway_client, gateway_embedder
from fcr_curator.lakebase import connect, ensure_vector_extension, synced_table_name
from fcr_curator.search import DEFAULT_LIMIT, NO_FILTERS, TEXT_SEARCH_VECTOR, ChunkSearch, SearchFilters, SearchHit


class LakebasePgVectorManager:
    """Embed a question and search the environment's synced knowledge base table with pgvector."""

    def __init__(
        self,
        config: ProjectConfig,
        env: str = "dev",
        workspace: WorkspaceClient | None = None,
        embed: EmbedBatch | None = None,
    ) -> None:
        """Search env's synced table; embed defaults to config's embedding model service on the gateway."""
        self.config = config
        self.workspace = workspace or WorkspaceClient()
        self.embed = embed or gateway_embedder(gateway_client(self.workspace), config.embedding_service)
        # Postgres drops the catalog: the table is <schema>.<table>
        self.table = synced_table_name(config, env).split(".", 1)[1]

    @property
    def text_index_name(self) -> str:
        """Return the full-text index's name, derived from the table's so each environment has its own."""
        return f"{self.table.split('.')[1]}_text_gin"

    def ensure_extension(self) -> None:
        """Install pgvector, which the synced table's vector column needs; does nothing if installed."""
        ensure_vector_extension(self.workspace, self.config)
        logger.info("pgvector extension ready")

    def ensure_indexes(self) -> None:
        """Create the full-text index keyword and hybrid search use; does nothing if it exists.

        Rerun it if the synced table is recreated, which drops its indexes. There's no vector index:
        an approximate one filters after it finds neighbours, so filtered searches could come back short,
        and exact scans are already fast at this size.
        """
        index = sql.SQL("CREATE INDEX IF NOT EXISTS {index} ON {table} USING gin (({text_vector}))").format(
            index=sql.Identifier(self.text_index_name),
            table=sql.Identifier(*self.table.split(".")),
            text_vector=sql.SQL(TEXT_SEARCH_VECTOR),
        )
        with connect(self.workspace, self.config) as connection:
            connection.execute(index)
        logger.info(f"Full-text index {self.text_index_name} ready on {self.table}")

    def embed_query(self, query: str) -> list[float]:
        """Embed query with the model that embedded the chunks, so their vectors are comparable."""
        [vector] = self.embed([query])
        if not vector:
            raise ValueError(f"The embedding model service {self.config.embedding_service} returned no vector")
        return vector

    def search(self, query: str, filters: SearchFilters = NO_FILTERS, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        """Return the chunks semantically closest to query, within filters."""
        query_vector = self.embed_query(query)
        with connect(self.workspace, self.config) as connection:
            return ChunkSearch(connection, self.table).semantic(query_vector, filters, limit)

    def hybrid_search(
        self, query: str, filters: SearchFilters = NO_FILTERS, limit: int = DEFAULT_LIMIT
    ) -> list[SearchHit]:
        """Return the semantic and keyword matches for query, within filters, fused by rank."""
        query_vector = self.embed_query(query)
        with connect(self.workspace, self.config) as connection:
            return ChunkSearch(connection, self.table).hybrid(query, query_vector, filters, limit)
