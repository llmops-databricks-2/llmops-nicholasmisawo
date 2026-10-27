# Bermuda Financial Condition Report Curator

**Databricks LLMOps | Bermuda Insurer and Reinsurer Financial Condition Intelligence**

An assistant for the Financial Condition Reports (FCRs) Bermuda commercial insurers and reinsurers
publish. It finds each company's report, and answers questions grounded in the figures the reports
disclose, with citations.
Built for the **LLMOps with Databricks** course.

## The problem

Under the Bermuda Monetary Authority's disclosure rules, commercial insurers and reinsurers publish an
annual FCR on business, governance, risk profile, solvency and capital. Each company posts its own PDF on
its own website in its own layout, so comparing companies or years means hunting down and reading dozens of long
reports. The assistant is for analysts, reinsurance buyers, and risk and compliance teams who follow the
market. It should answer questions like:

- How did Wilton Re's solvency ratio compare with its capital requirement in 2024?
- Which insurers and reinsurers reported the largest change in available statutory capital last year?
- How do Class 4 (re)insurers describe their exposure to catastrophe risk?

## How it works

```text
SerpAPI search -> bronze -> silver (curate, download PDFs) -> parse -> chunk -> embed -> Lakebase (pgvector)
                                                                                            |
           question -> rewrite -> semantic + keyword search (filtered) -> rerank -> token-budgeted context
```

One weekly job runs everything up to Lakebase. Search runs on demand. The design and what we found at
each stage are in [`docs/`](docs/README.md):

| Doc | Covers |
|---|---|
| [01 Data pipeline](docs/01-data-pipeline.md) | Finding FCRs on a search budget, curation, tables |
| [02 Chunking](docs/02-chunking.md) | Two chunking strategies compared, boundaries, the embedding window |
| [03 Lakebase](docs/03-lakebase.md) | Embeddings and the synced pgvector table |
| [04 Search](docs/04-search.md) | Filtered, keyword and hybrid search, reranking, rewriting, token budget |
| [05 Layers and dbt](docs/05-layers.md) | Raw, bronze, silver, gold; what dbt builds; migration phases |
| [Context engineering](docs/context-engineering.md) | Every strategy that shapes what the model sees |
| [Environment](docs/environment.md) | Resources, model services, environments, CI |

## Quick start

```bash
uv sync --extra dev --extra ci          # dependencies, including pytest and pre-commit
uvx pre-commit install                  # ruff and hygiene checks on every commit
uv run pytest                           # unit tests
```

Once per workspace, store the SerpAPI key as a secret:

```bash
databricks secrets create-scope fcr_curator --profile <profile>
databricks secrets put-secret fcr_curator serpapi_api_key --profile <profile>
```

Deploy and run:

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run fcr_pipeline -t dev --only bronze_fcr_documents --params dry_run=true   # plan and cost, no searches
databricks bundle run fcr_pipeline -t dev                                                     # the whole pipeline
databricks bundle run fcr_pipeline -t dev --only silver_fcr_documents+                        # everything after search
```

Search the chunks from your machine:

```bash
env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/03_retrieval/search_fcr_chunks.py
```

## Project layout

```text
src/fcr_curator/     the package: search planning, curation, chunking, embeddings, Lakebase, search
notebooks/
  01_ingestion/      bronze (search) and silver (curate, download)
  02_processing/     parse, chunk, embed, compare chunking strategies
  03_retrieval/      Lakebase sync, search walkthrough, UI search queries (search_examples.sql)
dbt/                 dbt project (own environment): seed, staging, intermediate, context, marts
resources/           the bundle's job definition
tests/               unit tests, one file per module
docs/                design and findings
databricks.yml       bundle targets dev, acc and prd
project_config.yml   catalog, schema, volume and model services per environment
```

## Course community

Join the [LLMOps course Discord](https://discord.gg/V4XRYcBFDE) with the Discord username given at
registration.
