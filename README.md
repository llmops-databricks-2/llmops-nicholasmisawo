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
   dbt                     Python                         dbt          Python           dbt
entity list   -> plan -> search + disclosures pages -> curate -> download PDFs -> parse -> chunk -> embed -> knowledge base
  (BMA)   fcr_search_plan  raw_fcr_search_hits            fcr_register  (volume)      fcr_parsed_documents        fcr_knowledge_base
                                                                                                                |
                                                                                              Lakebase (pgvector) copy
                                                                                                                |
              question -> rewrite -> semantic + keyword search (filtered) -> rerank -> cited, token-budgeted context
```

Python does only what dbt can't: calling SerpAPI, reading disclosures pages and downloading PDFs. It
appends what it saw to tables dbt defines, never updating a row; dbt plans the searches and works out
everything else.

One weekly job runs everything up to Lakebase. Search runs on demand. The entity list is the BMA's
licensed Class 4, Long Term E, Class 3B and group insurers; every FCR kept is named after one of them,
and every citation opens the PDF at the cited page. The design
and what we found at each stage are in [`docs/`](docs/README.md):

| Doc | Covers |
|---|---|
| [01 Environment](docs/01-environment.md) | Resources, model services, environments, CI |
| [02 Layers and dbt](docs/02-layers-and-dbt.md) | The layers, the dbt models and where the dbt context engineering package is used |
| [03 Data pipeline](docs/03-data-pipeline.md) | Finding FCRs on a search (Google SerpAPI) budget, curation, tables |
| [04 Chunking](docs/04-chunking.md) | Two chunking strategies compared, boundaries, the embedding window |
| [05 Lakebase](docs/05-lakebase.md) | Embeddings and the synced pgvector copy of the knowledge base |
| [06 Search](docs/06-search.md) | Filtered, keyword and hybrid search, reranking, rewriting, token budget |
| [07 Context engineering](docs/07-context-engineering.md) | Every strategy that shapes what the model sees |

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
databricks bundle run fcr_pipeline -t dev --only dbt_search_plan,search_fcrs --params dry_run=true   # plan and cost, no searches
databricks bundle run fcr_pipeline -t dev                                                     # the whole pipeline
databricks bundle run fcr_pipeline -t dev --only dbt_register+                                # everything after search
```

The job runs dbt itself. To run dbt from your machine, see [02 Layers and dbt](docs/02-layers-and-dbt.md#running-dbt).

Search the knowledge base from your machine:

```bash
env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/02_retrieval/search_knowledge_base.py
```

## Project layout

```text
src/fcr_curator/     the package: search planning, sources, curation, downloads, chunking, gateway, Lakebase, search
notebooks/
  01_ingestion/      search_fcrs (SerpAPI and pages), download_fcrs
  02_retrieval/      sync to Lakebase, search walkthrough, UI search queries (search_examples.sql)
dbt/                 dbt project (own environment): reference seed, curation, parsing, chunking, embeddings, marts
resources/           the bundle's job definition
tests/               unit tests, one file per module
docs/                design and findings
databricks.yml       bundle targets dev, acc and prd
project_config.yml   catalog, schema, volume and model services per environment
```

## Course community

Join the [LLMOps course Discord](https://discord.gg/V4XRYcBFDE) with the Discord username given at
registration.
