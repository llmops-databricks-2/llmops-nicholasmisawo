# 05 Layers and dbt

## The layers

A file lands untouched; a parser makes it readable; chunking and embedding make it retrievable; one
schema makes it answerable. The dbt project is laid out like dbt Labs'
[jaffle-logistics](https://github.com/dbt-labs/jaffle-logistics) example, which maps onto those layers:

| Layer | Meaning | Where | Tables |
|---|---|---|---|
| **Raw** | Lands untouched | Python | PDFs in the `fcr_documents` volume; `bronze_fcr_documents`, `bronze_fcr_search_log` |
| **Bronze** | Machine-readable | Python, then dbt `staging/` | `silver_parsed_fcr_documents` (parser output) → `stg_fcr_*` views |
| **Silver** | Retrievable | dbt `intermediate/`, `context/` | `int_fcr_documents` → `fcr_parsed_hashed` → `chunk_fcr_sections` → `fcr_chunks_meta` |
| **Gold** | Answerable | dbt `context/ai/`, `marts/` | `fcr_chunk_embeddings` → **`fcr_knowledge_base`**; `dim_fcr_companies` |

Reference data: **`ref_bermuda_reinsurers`**, the dbt seed of the reference list (`dbt/seeds/`).

## The dbt models

```text
dbt/models/
  staging/       stg_fcr_documents, stg_fcr_parsed_documents, stg_fcr_reinsurers       (SQL views)
  intermediate/  int_fcr_documents: each FCR with its company and reference details    (SQL view)
  context/       fcr_parsed_hashed       content_hash of each parse (package macro)    (SQL)
                 chunk_fcr_sections      our section chunker, fcr_curator.chunking     (Python)
                 fcr_chunks_meta         each chunk with its FCR's current metadata    (SQL)
    ai/          fcr_chunk_embeddings    course_ops.gateway.embed, through gateway.py  (Python)
                 fcr_chunks_embedded     each chunk with the vector for its text       (SQL)
                 fcr_knowledge_base      the package's knowledge_base serving shape    (SQL)
  marts/         dim_fcr_companies       FCR coverage by company and reporting year    (SQL view)
```

## Where the dbt_context_engineering package is used, and where it isn't

| Package piece | Used? | Why |
|---|---|---|
| `content_hash` | Yes (`fcr_parsed_hashed`) | Tells changed parses from unchanged ones |
| `knowledge_base` | Yes (`fcr_knowledge_base`) | One serving shape with a first-class `citation_url`, here the FCR opened at the cited page (`#page=N`); incremental, only new or changed rows |
| `chunk` | No: **our section chunker**, as a Python model | It packs units by token count; ours follows the FCR's sections and measured far better ([02 Chunking](02-chunking.md)) |
| `embed` | No: **a Python model on `course_ops.gateway.embed`** | Its Databricks version is SQL `ai_query`, which can't reach a model service the workspace created |
| `attach_metadata` | No: plain SQL join (`fcr_chunks_meta`) | Its output keeps only the package's chunk columns, which would drop `page_id`, needed for citations |

## Each text is embedded once

`fcr_chunk_embeddings` reuses any vector already made for the same chunk id and content hash, in its own
table or in the Python pipeline's (`gold_fcr_chunks_by_section`), and sends only new or changed text to
the gateway. The section chunker is the same code the pipeline runs, so ids and text match, and the first
dbt run should embed nothing.

## Running dbt

In the job, `dbt_seed` runs first and `dbt_context` after processing, both on the SQL warehouse; the
Python models run on serverless with the deployed `fcr_curator` wheel installed (passed as the
`fcr_curator_wheel` var). Locally, from `dbt/` (signs in through the browser):

```bash
uv sync
uv run dbt deps
uv run dbt build --select bermuda_reinsurers
uv run dbt build --select +fcr_knowledge_base dim_fcr_companies --vars '{fcr_curator_wheel: <workspace path of the wheel>}'
```

dbt-core reports `CustomKeyInConfigDeprecation` for `environment_key`, `environment_dependencies` and
`embedding_endpoint` on the Python models. The first two are dbt-databricks settings, read from that
place; the third has to be a plain config key, because a Python model sees `meta` as empty. They stay.

## Migration

| Step | Status |
|---|---|
| Reference list as a dbt seed; Python reads `ref_bermuda_reinsurers` | Built |
| Staging, intermediate, context, knowledge base and marts in dbt | Built |
| Parsing (`ai_parse_document`) in dbt, so dbt reads the volume directly | Next |
| Point the Lakebase synced table at `fcr_knowledge_base` and adapt `search.py` to its shape, then retire the Python chunking and embedding | Needs explicit approval: it deletes the current synced table |
