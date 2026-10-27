# Databricks notebook source
# MAGIC %md
# MAGIC # Search the FCR knowledge base in Lakebase
# MAGIC
# MAGIC One question, taken through each retrieval technique in turn so the effect of each shows:
# MAGIC
# MAGIC 1. **Query rewriting**: the chat model turns the question into search text plus filters
# MAGIC 2. **Semantic search**, without and then with the filters
# MAGIC 3. **Keyword search** with the filters
# MAGIC 4. **Hybrid search**: semantic and keyword fused with Reciprocal Rank Fusion
# MAGIC 5. **Reranking**: the chat model reorders the hybrid candidates by relevance
# MAGIC 6. **Token budget**: the best hits packed into a cited context that fits the budget
# MAGIC
# MAGIC Run it from your machine, not as a job: it connects to Postgres with psycopg, whose compiled library
# MAGIC aborts Python on serverless job compute.
# MAGIC
# MAGIC     env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/03_retrieval/search_knowledge_base.py

# COMMAND ----------

from datetime import date

from databricks.sdk import WorkspaceClient

from fcr_curator.config import load_config
from fcr_curator.context_budget import build_context
from fcr_curator.gateway import gateway_chat, gateway_client, gateway_embedder
from fcr_curator.lakebase import connect, synced_table_name
from fcr_curator.query_rewriting import rewrite_query
from fcr_curator.reranking import rerank
from fcr_curator.search import ChunkSearch, SearchHit

# COMMAND ----------
# Settings

ENV = "dev"
QUESTION = "How did Wilton Re's solvency ratio compare with its capital requirement in 2024?"
CANDIDATES = 20  # Hits each search returns, and the reranker reads
TOP_K = 5  # Hits kept after reranking
MAX_CONTEXT_TOKENS = 1500

cfg = load_config("project_config.yml", env=ENV)
workspace = WorkspaceClient()
client = gateway_client(workspace)
chat = gateway_chat(client, cfg.llm_endpoint)
embed = gateway_embedder(client, cfg.embedding_endpoint)
postgres_table = synced_table_name(cfg, ENV).split(".", 1)[1]  # Postgres drops the catalog: <schema>.<table>


def show(title: str, hits: list[SearchHit]) -> None:
    """Print the top hits of one search, one line each."""
    print(f"\n{title}")
    for rank, hit in enumerate(hits[:TOP_K], start=1):
        print(f"  {rank}. {hit.company} {hit.reporting_year} page {hit.page} | {hit.text[:90]!r}")


# COMMAND ----------
# 1. Query rewriting

rewritten = rewrite_query(QUESTION, chat, today=date.today())
query_vector = embed([rewritten.search_text])[0]
print(f"Question: {QUESTION}\nSearch text: {rewritten.search_text}\nFilters: {rewritten.filters}")

# COMMAND ----------
# 2-4. Semantic (without and with filters), keyword and hybrid search

with connect(workspace, cfg) as connection:
    search = ChunkSearch(connection, postgres_table)
    show("Semantic, no filters", search.semantic(query_vector, limit=CANDIDATES))
    show("Semantic, filtered", search.semantic(query_vector, rewritten.filters, CANDIDATES))
    show("Keyword, filtered", search.keyword(rewritten.search_text, rewritten.filters, CANDIDATES))
    candidates = search.hybrid(rewritten.search_text, query_vector, rewritten.filters, CANDIDATES)
    show("Hybrid (RRF), filtered", candidates)

# COMMAND ----------
# 5. Reranking

reranked = rerank(QUESTION, candidates, chat, top_k=TOP_K)
show("Reranked", reranked)

# COMMAND ----------
# 6. Token budget

context = build_context(reranked, max_tokens=MAX_CONTEXT_TOKENS)
print(f"\nContext: {len(context.sources)} of {len(reranked)} hits, ~{context.tokens} of {MAX_CONTEXT_TOKENS} tokens\n")
print(context.text)
