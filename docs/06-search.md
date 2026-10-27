# 06 Search

One question goes through every retrieval technique in `notebooks/02_retrieval/search_knowledge_base.py`,
so the effect of each shows. It searches the Lakebase copy of the dbt knowledge base
(`nicholasmisawo.fcr_knowledge_base_lakebase_<target>`) over Postgres, so run it from your machine:

```bash
env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/02_retrieval/search_knowledge_base.py
```

| Step | Module | What it does |
|---|---|---|
| Query rewriting | `query_rewriting.py` | The chat model rewrites the question in FCR terms and pulls out filters ((re)insurer, year; "last year" resolved against today) |
| Semantic search | `search.py` | pgvector cosine distance (`<=>`); cosine because the embeddings aren't normalised |
| Filters | `search.py` | (Re)insurer matched loosely on the company (`account_key`) and the citation URL, which holds the domain (`Wilton Re` finds `wiltonre.bm`); year from `ts` |
| Keyword search | `search.py` | Postgres full-text search for any of the words, ranked by `ts_rank_cd` |
| Hybrid search | `search.py` | Both rankings fused with Reciprocal Rank Fusion (k = 60) |
| Reranking | `reranking.py` | One chat call reorders the 20 hybrid candidates by usefulness, keeping the top 5 |
| Token budget | `context_budget.py` | The best hits packed under 1,500 estimated tokens, each cited with (re)insurer, year, page and a link that opens that page |

## Findings: what each step changed

Question: *"How did Wilton Re's solvency ratio compare with its capital requirement in 2024?"*

| Step | Top results | What it shows |
|---|---|---|
| Query rewriting | Search text `solvency ratio capital requirement`; filters `Wilton Re`, `2024` | The (re)insurer and year become filters. Left in the text, they dominate the vector: *"Wilton Re eligible capital… 2024"* ranked Wilton Re's cover, risk and investment pages first |
| Semantic, no filters | Lloyd's, Steamship Mutual, Vienna Insurance Group, HSBC | The right meaning but the wrong companies, all in Solvency II wording |
| Semantic, filtered | Wilton Re 2024: Regulatory Capital Requirements table (page 16) first | Filters alone put every hit in the right report |
| Keyword, filtered | Wilton Re 2024: Eligible Capital text (page 15) first | Exact terms rank differently; the requirement table drops to 4th |
| Hybrid (RRF) | The requirement text and table (page 16), then Eligible Capital | Chunks both searches found rise to the top |
| Reranked | Eligible Capital table ($1,757.3M), ECR table ($717.1M), compliance statement, tier rules | The two tables that answer the question come first |
| Token budget | 4 cited sources, about 845 of 1,500 tokens | Only what answers the question reaches the context |

**The answer the context supports**: total eligible capital of $1,757.3M against an ECR of $717.1M, so
Wilton Re's 2024 cover was about 245%.

## Findings: problems met

- **The reranker first dropped every chunk.** Wilton Re never states a ratio, only the figures to work it out from, and "leave out excerpts that don't help" was read strictly. The prompt now counts excerpts holding the figures needed as useful.
- **Results vary slightly between runs**, because the chat model isn't deterministic: the rewritten text and the number of reranked hits changed. Setting `temperature=0` is untested.
- **EU/UK reports in the index** show up whenever filters are off (see [03 Data pipeline](03-data-pipeline.md#findings)).

## Testing from the UI

`notebooks/02_retrieval/search_examples.sql` has the same searches as SQL, with no Python needed. Each
returns the company, reporting year, an excerpt and the `citation_url` that opens the cited page.

| Query | Editor | Expected top result |
|---|---|---|
| 1. Semantic, filtered (delete the `WHERE` line for unfiltered) | Databricks SQL editor | Regulatory Capital Requirements table, page 16, similarity about 0.79 |
| 2. The question's embedding, to copy | Databricks SQL editor | A list of 1,024 numbers |
| 3. Keyword, filtered | Lakebase SQL editor | Eligible Capital text, page 15 |
| 4. Hybrid (RRF), with the vector from query 2 pasted in | Lakebase SQL editor | Regulatory Capital Requirements chunks, page 16 |

The Databricks SQL editor can embed a question because `system.ai.gte-large-en` is the same model as
`course_ops.gateway.embed`: the same sentence through both gave cosine similarity 1.0. Query rewriting,
reranking and the token budget need the chat model, so they're only in the Python notebook.
