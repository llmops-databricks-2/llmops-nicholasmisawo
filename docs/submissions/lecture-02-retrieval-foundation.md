# Lecture 2 submission: the retrieval foundation

Submitted 2026-10-28. **Project:** an assistant over the Financial Condition Reports (FCRs) that
BMA-licensed insurers and reinsurers publish. It covers 194 entities (Class 4, Long Term Class E, Class
3B and insurance groups); 86 FCRs from 45 of them are currently registered.

## 1. Processed documents and chunk tables

Each FCR is parsed once with `ai_parse_document` into `fcr_parsed_documents`, and the parse is never
repeated, even on a full refresh. Across the first 78 FCRs the parser returned:

| Element | Count | Use |
|---|---|---|
| Text | 16,241 | Chunked |
| Section headers | 15,421 | Chunk headings |
| Tables | 3,496 | Chunked, kept as HTML |
| Page headers, footers, numbers | 10,916 | Dropped (repeated on every page) |
| Figures | 2,004 | Dropped (descriptions disabled to save cost) |

The chunk tables are:

- **`chunk_fcr_sections`**: the chunks
- **`fcr_chunks_meta`**: each chunk with its insurer, licence class, business, reporting year, title,
  URL and page
- **`fcr_chunks_embedded`**: each chunk with its 1,024-dimension vector

dbt tests check the tables on every run: unique ids, no empty text, every vector exactly 1,024 numbers,
and no orphan rows.

## 2. Chunk sizes and an alternative strategy

**Per-element chunking** (the baseline) was compared with **section chunking**. Section chunking starts
every chunk with its section heading, packs text up to 2,000 characters split at sentence ends, merges
sections under 200 characters, and splits tables by rows with the header row repeated.

| | Per element | **Per section (adopted)** |
|---|---|---|
| Chunks | 19,737 | 18,045 |
| Median / average (characters) | 454 / 782 | 773 / 935 |
| Under 50 characters | 1,869 | 69 (−96%) |
| Over 2,000 characters | 1,594 | 192 (−88%) |
| Largest | 35,926 | 3,548 |

- **Noise:** section chunking removed it (lone company names, stray labels), and every table now carries
  its section heading.
- **Oversize:** 183 of the 192 chunks still over 2,000 characters are tables whose single row is already
  that long.
- **Overlap:** none is used. 89% of text chunks don't continue a section, and split passages were both
  retrieved by the reranker.
- **Embedding window:** the largest chunk uses about a quarter of gte-large-en-v1.5's 8,192-token window.
  Per-element chunking's largest table (about 12–18k tokens) would have been cut off.

## 3. Lakebase synced table

`fcr_knowledge_base_lakebase_dev` is a synced copy of the dbt knowledge base `fcr_knowledge_base`:

| Setting | Value |
|---|---|
| Postgres table | `nicholasmisawo.fcr_knowledge_base_lakebase_dev` |
| Primary key | `source_key` |
| Embedding | pgvector `vector(1024)` |
| Metadata | `account_key` (insurer), `ts` (reporting year end), `citation_url` (the FCR at the cited page), `classification` (the insurer's licence class) |
| Scheduling | TRIGGERED, refreshed by the pipeline job through Change Data Feed |
| State | ONLINE: 14,192 chunks from 44 insurers, matching the Delta table |

Three lessons from setting it up:

- The sync needs the pgvector extension, installed once.
- `psycopg` aborts Python on serverless job compute, so queries run from a client.
- The first snapshot completes about 15 minutes after the job reports success.

## 4. Semantic, filtered and hybrid search

Test question: *"How did Wilton Re's solvency ratio compare with its capital requirement in 2024?"*

| Step | Result | What changed |
|---|---|---|
| Query rewriting | Search text `solvency ratio capital requirement`; filters Wilton Re, 2024 | The name and year move out of the text and into filters; left in, they dominated the embedding |
| Semantic, unfiltered | Lloyd's, Steamship Mutual, HSBC | Right meaning, wrong companies |
| Semantic, filtered | Regulatory Capital Requirements table (p. 16) first | Every hit from the right report |
| Keyword, filtered | Eligible Capital text (p. 15) first | Exact terms rank differently |
| Hybrid (Reciprocal Rank Fusion, k=60) | Requirement text and table, then Eligible Capital | Hits found by both searches rise to the top |
| Reranked | Eligible Capital ($1,757.3M) and ECR ($717.1M) tables first | The answering tables lead |
| Token budget | 4 cited sources, about 845 of 1,500 tokens | Only relevant context is kept |

**Answer supported by the context:** Wilton Re's 2024 eligible capital of $1,757.3M against an ECR of
$717.1M, a cover of about 245%.

How the searches work:

- Semantic search uses pgvector cosine distance.
- Keyword search uses Postgres full-text search.
- Filters cover insurer, reporting year and licence class.

The same searches also exist as SQL (`notebooks/02_retrieval/search_examples.sql`): queries 1–2 for the
Databricks SQL editor, 3–5 for the Lakebase SQL editor.

## 5. Pull requests

| PR | Scope | Status |
|---|---|---|
| #6 | Lakebase synced table and hybrid search over FCR chunks | Merged 2026-10-26 |
| #7 | FCR pipeline in dbt on the BMA's licensed-insurer list | Merged 2026-10-27 |

CI (pre-commit hooks and 181 unit tests) passed on both merges.

**Supporting documentation:** [04 Chunking](../04-chunking.md), [05 Lakebase](../05-lakebase.md),
[06 Search](../06-search.md)
