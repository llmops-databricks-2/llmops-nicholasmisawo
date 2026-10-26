# Financial Condition Report Curator

**Databricks LLMOps | Bermuda Insurer Financial Condition Intelligence**

An AI-powered assistant that curates the Financial Condition Reports (FCRs) Bermuda commercial insurers publish. It helps analysts, risk, and compliance teams find an insurer's report, compare institutions and years, and get answers grounded in the figures each report discloses.

Built as part of the **LLMOps with Databricks** course, the project demonstrates end-to-end ingestion, governance, retrieval-augmented generation (RAG), evaluation, and monitoring.

## 1. Project Overview

### User and Problem

The application supports credit and equity analysts, reinsurance buyers, risk managers, and compliance teams who follow the Bermuda insurance market.

Under the Bermuda Monetary Authority's public disclosure rules, commercial insurers publish an annual FCR covering their business and performance, governance, risk profile, solvency valuation, and capital management. Each insurer posts its own PDF on its own website, in its own layout, so there's no single place to find them, and comparing institutions or years means hunting down and reading dozens of long reports.

### Data Sources

| Source | Dataset | Purpose |
|---|---|---|
| Google search results via [SerpAPI](https://serpapi.com/search-api) | FCR PDFs published on insurers' websites | Discover each insurer's FCR for each reporting year |

Insurers host their FCRs themselves rather than through the BMA, so the pipeline finds them by search instead of scraping one listing. A new source goes in `sources/<name>.py` and returns rows matching `SOURCE_SCHEMA`, so a registry of known FCR pages, for example, can be added beside search later.

### Example Questions

- What was Example Re Ltd.'s BSCR ratio at the end of 2024, and how did it change from 2023?
- Which insurers reported the largest change in available statutory capital last year?
- How do Class 4 insurers describe their exposure to catastrophe risk?
- Which FCRs report a change in auditor or approved actuary?
- Which insurers have no FCR for the latest reporting year yet?

The assistant combines RAG-based document retrieval with SQL over document metadata to generate evidence-grounded responses with source citations.

## 2. Architecture

**Data ingestion**
- SerpAPI runs Google searches for FCR PDFs, generic per reporting year plus optional per-institution queries, and records every query that found each URL and its best rank.
- Curation classifies each result (FCR, SFCR or other), extracts the institution and reporting year, and keeps one document per institution and year.
- Delta tables store the document metadata.
- Unity Catalog Volumes store the downloaded source PDFs.

**Retrieval and analytics**
- Document extraction with `ai_parse_document`, cleaning, chunking, and embeddings.
- Semantic retrieval over FCR text and tables.
- SQL filtering by institution, domain, and reporting year.

**LLMOps**
- Databricks AI Gateway for chat and embedding models.
- MLflow for experiments, evaluation, tracing, and monitoring.
- Databricks Asset Bundles for environment-specific deployment.
- GitHub Actions for CI/CD.

## 3. Data Pipelines

One job, `fcr_pipeline-<target>`, runs the stages in order; each task starts only once the one before it succeeds. Every task is a serverless notebook with the packaged project wheel installed and environment-specific settings from `project_config.yml`:

| Task | Notebook | Unity Catalog tables | Volume folder |
|---|---|---|---|
| `bronze_fcr_documents` | `01_ingestion/bronze_fcr_documents.py` | `bronze_fcr_documents` | — |
| `silver_fcr_documents` | `01_ingestion/silver_fcr_documents.py` | `silver_fcr_documents` | `fcr_documents/<source>/` (only when `DOWNLOAD_FILES = True`) |
| `process_fcr_documents` | `02_processing/process_fcr_documents.py` | `silver_parsed_fcr_documents`, `gold_fcr_chunks` | — (reads the silver downloads) |

The job runs weekly, Mondays at 06:00 Bermuda time (`Atlantic/Bermuda`). The schedule is paused in `dev` and `acc` and active in `prd`, set by the `schedule_pause_status` bundle variable in `databricks.yml`; the usage policy comes from the `usage_policy_id` variable. Run it by hand with:

```bash
databricks bundle run fcr_pipeline -t dev
```

To run a single stage, start a run from the job page and choose that task. Each stage needs the one before it to have run at least once.

### SerpAPI key

Bronze reads the SerpAPI key from a Databricks secret, never from `project_config.yml`. The scope and key name default to `fcr_curator` / `serpapi_api_key` (`serpapi_secret_scope` and `serpapi_secret_key` in `ProjectConfig`). Create them once per workspace:

```bash
databricks secrets create-scope fcr_curator --profile <profile>
databricks secrets put-secret fcr_curator serpapi_api_key --profile <profile>
```

### Search budget

The SerpAPI plan allows 1,000 searches a month. Every successful search counts, including a page with no results; cached and failed searches don't. Bronze spends them only where a new FCR PDF is likely, using the rules in `src/fcr_curator/search_plan.py`:

| Priority | Query | Asked when | Pages |
|---|---|---|---|
| 1. `missing_year` | `site:<domain> "financial condition report" <year> filetype:pdf` | A domain in silver has an FCR, but not one for the latest reporting year | 1 |
| 2. `institution` | `"<name>" "financial condition report" filetype:pdf` | A name in `INSTITUTIONS` has no FCR in silver for the latest year | 1 |
| 3. `discovery` | Two generic templates per year in `DISCOVERY_YEARS` | Always, subject to the requery wait; the only way to find new domains | 2 |

- **No repeat searches.** Every billed search is appended to `bronze_fcr_search_log`. A query isn't asked again for `REQUERY_AFTER_DAYS` (28), or `DEAD_QUERY_REQUERY_DAYS` (90) when its last run found nothing that looked like an FCR.
- **No wasted pages.** A second page is asked only when the first was full and held at least one FCR-like result.
- **Hard caps.** Before searching, the run checks the account's searches left through SerpAPI's free Account API, then spends at most `min(RUN_SEARCH_CAP, searches left - SEARCH_RESERVE)`: 150 a run, keeping 100 back for manual and dev runs. Weekly runs at the cap spend at most about 650 a month. The account check sees usage from every environment and every manual run sharing the key; the search log is per environment.
- **Dry run.** Run the job with `--params dry_run=true` to print the plan, its worst-case cost and the run's budget without searching (see [Validate, deploy, and run](#validate-deploy-and-run)).
- **Nothing lost on failure.** If SerpAPI fails partway (an exhausted plan, say), results and log entries from the searches already made are kept, then the task fails.

**Bronze** plans the queries, runs them through `src/fcr_curator/sources/serpapi.py`, logs each search, merges results that point at the same URL, and upserts them into bronze. It downloads nothing.

**Silver** curates all of bronze with the rules in `src/fcr_curator/curation.py` and upserts the result into silver:

- Keeps PDFs only: a `.pdf` URL, or one with no extension that a `filetype:pdf` query found.
- Classifies each result as `fcr`, `sfcr` (the EU/UK Solvency and Financial Condition Report) or `other` from its title and filename, falling back to the snippet.
- Extracts the reporting year, preferring a year beside the report's name (`FCR 2024`) over any other, so an upload date in a filename doesn't win. Years before 2016, when the disclosure rules took effect, and the current year, which can't have been reported on yet, are ignored.
- Takes the institution from the title before the report's name, falling back to the site's name as Google gives it.
- Keeps one document per institution and reporting year on each domain, preferring one search still returns and then the best-ranked, recording the dropped ids in `superseded_ids`.

New documents arrive with `processed` and `volume_path` as `None`, following the course's `arxiv_papers` pattern, until the notebook's download section fills them in. That section is gated by `DOWNLOAD_FILES` (currently `True`) and downloads documents with no `volume_path` yet into the volume as `<source>/<document_id>.pdf`; `MAX_DOWNLOADS` caps new attempts per run (currently `None`). Silver keeps only the kinds in `INCLUDED_DOC_KINDS` (currently `fcr`) reporting on `MIN_REPORTING_YEAR` or later (currently `2020`; `None` keeps every year). While a year is set, documents with no reporting year are left out too. An unknown kind or an empty set fails the run rather than silently dropping documents.

Each download is checked for a PDF header; a file that isn't one (a landing or login page) is removed and counted as rejected. Insurer websites block or move files more often than a regulator's CDN does, so the run lists the documents it couldn't download; they're retried on the next run at no search cost.

### Medallion tables and incremental runs

| Table | Layer | Holds | Written by |
|---|---|---|---|
| `bronze_fcr_documents` | bronze | Every search result any run has found, plus `first_seen_at` and `is_listed` | Bronze task |
| `bronze_fcr_search_log` | bronze | One row per billed SerpAPI search: query, reason, page, results and FCR-like results; append-only | Bronze task |
| `silver_fcr_documents` | silver | Curated FCRs: PDFs only, classified, one per institution and year, filtered to `INCLUDED_DOC_KINDS` reporting on `MIN_REPORTING_YEAR` or later, plus `processed` and `volume_path` | Silver task |
| `silver_parsed_fcr_documents` | silver | `ai_parse_document` output (as JSON) for each downloaded FCR, parsed once | Process task |
| `gold_fcr_chunks` | gold | Cleaned text and table chunks with page, document metadata and embedding; Change Data Feed on for a Vector Search index | Process task |

Each table carries a `COMMENT` and a `quality` table property (`bronze` / `silver` / `gold`), so its layer shows in Catalog Explorer. All are updated incrementally rather than overwritten, so runs are safe to repeat:

- **Bronze** inserts new results and updates only rows whose content changed or that a new query found; `search_queries` keeps every query that ever found the row. A new run timestamp or a new `search_rank` alone is not a change, since Google reorders results between runs, so unchanged rows and their `first_seen_at` are left alone. A run asks only some queries, so a row is marked `is_listed = false` only when every one of its queries was asked again and none returned it; it flips back if it reappears.
- **Silver** is re-curated from all of bronze each run (deduplication needs every document) and upserted the same way. Its `MERGE` never writes `processed` or `volume_path`, so downloads survive re-ingestion. Documents that no longer survive curation are removed from silver; any file already downloaded for them stays in the volume.
- **Downloads**, when enabled, fetch only rows with no `volume_path` and skip files already in the volume.
- **Processing** parses only downloaded documents not yet in `silver_parsed_fcr_documents` and chunks only parsed documents not yet in `gold_fcr_chunks`, so each FCR is parsed and embedded once. Parsed rows and chunks of documents silver no longer holds are deleted, so retrieval never answers from a superseded duplicate.

### Bronze table schema: `bronze_fcr_documents`

| Column | Type | Description |
|---|---|---|
| `document_id` | string | `serpapi_<hash>`, a hash of the normalized result URL |
| `title` | string | Result title as Google shows it |
| `snippet` | string | Result snippet |
| `pdf_url` | string | Result URL, with the host lower-cased and any fragment dropped |
| `displayed_link` | string | URL as Google displays it |
| `site_name` | string | The result's site as Google names it |
| `domain` | string | Result host without `www.` |
| `search_queries` | array\<string\> | Every query that ever found the URL, sorted |
| `search_rank` | int | Best position across those queries, 1-based |
| `source` | string | `serpapi` |
| `ingestion_timestamp` | string | Ingest run that last inserted or changed the row (UTC ISO) |
| `first_seen_at` | string | Ingest run that first inserted the row (UTC ISO) |
| `is_listed` | boolean | `false` once every query that found the URL was asked again without returning it |

### Silver table schema: `silver_fcr_documents`

| Column | Type | Description |
|---|---|---|
| `document_id` | string | Bronze `document_id` |
| `source` | string | `serpapi` |
| `institution` | string | Title before the report's name, else `site_name` |
| `domain` | string | Result host without `www.` |
| `title` | string | Normalized title |
| `doc_kind` | string | `fcr`, `sfcr` or `other` |
| `file_type` | string | File extension, always `pdf` for now |
| `file_url` | string | Result URL |
| `reporting_year` | int | Year the FCR reports on, if found |
| `snippet` | string | Result snippet |
| `search_queries` | array\<string\> | Every query that found the URL |
| `search_rank` | int | Best position across those queries |
| `is_listed` | boolean | `false` once every query that found the URL was asked again without returning it |
| `superseded_ids` | array\<string\> | Ids of duplicates this row replaced |
| `ingestion_timestamp` | string | Ingest run that last changed the bronze row (UTC ISO) |
| `first_seen_at` | string | Ingest run that first found the document (UTC ISO) |
| `processed` | long | Download time as `yyyyMMddHHmm`; `None` until downloaded (`DOWNLOAD_FILES = True`) |
| `volume_path` | string | File location in the volume; `None` until downloaded (`DOWNLOAD_FILES = True`) |

## 4. Provisioned Databricks Resources

| Resource | Value |
|---|---|
| Development schema | `llmops_dev.nicholasmisawo` |
| Acceptance schema | `llmops_acc.nicholasmisawo` |
| Production schema | `llmops_prd.nicholasmisawo` |
| Project document volume | `fcr_documents` (created by the silver job when `DOWNLOAD_FILES = True`) |
| SQL warehouse ID | `0e56acba898dad4a` |
| Usage policy ID | `ffe08ae3-a783-3201-ab0c-2fff24538807` |
| Dev MLflow experiment | `/llmops/nicholasmisawo-dev` |
| Acc MLflow experiment | `/llmops/nicholasmisawo-acc` |
| Prd MLflow experiment | `/llmops/nicholasmisawo-prd` |
| Chat model service | `course_ops.gateway.chat` |
| Embedding model service | `course_ops.gateway.embed` |

### AI Gateway

The course provides two governed AI Gateway model services:

- **Chat:** GPT-OSS-120B
- **Embeddings:** GTE Large

Example:

```python
from databricks_openai import DatabricksOpenAI

client = DatabricksOpenAI(use_ai_gateway=True)

response = client.chat.completions.create(
    model="course_ops.gateway.chat",
    messages=[
        {
            "role": "user",
            "content": "Summarise the capital management section of a Bermuda insurer's FCR.",
        }
    ],
    reasoning_effort="low",
    max_tokens=1024,
)

embeddings = client.embeddings.create(
    model="course_ops.gateway.embed",
    input="Bermuda Class 4 insurer BSCR ratio",
)
```

The repository dependencies don't currently include the `databricks-openai` package. The APIs can also be reached through the OpenAI client, configured with Databricks authentication and the `/ai-gateway/mlflow/v1` base URL.

The `embedding_endpoint` configuration identifies an AI Gateway embedding service, not a managed Vector Search embedding endpoint.

## 5. Lakebase and Vector Storage

The `lakebase_instance` in `project_config.yml` identifies the provisioned Lakebase project shared across development, acceptance, and production.

Unity Catalog schemas remain environment-specific.

The course does not automatically create an application database or install `pgvector`. These resources must be configured during the relevant lecture.

Lakebase supports PostgreSQL-based vector storage. Databricks managed Vector Search would instead need its own endpoint and index.

Provisioned Lakebase characteristics include:

- OAuth PostgreSQL roles and project-management permissions for authorized principals.
- Student usage policy and cost tagging.
- Initial compute range of 0.5–2 CU.
- Suspension after five idle minutes.

Additional branches or compute resources may increase costs.

## 6. Repository Structure

```text
llmops-nicholasmisawo/
├── src/
│   └── fcr_curator/
│       ├── config.py
│       ├── curation.py        # rules that turn bronze results into silver FCRs
│       ├── data_processor.py  # parse, chunk and embed downloaded FCRs
│       ├── download.py        # download curated FCRs into the volume
│       ├── http_client.py     # retrying HTTP session and file download
│       ├── schemas.py         # Spark schemas of the medallion tables
│       ├── search_plan.py     # choose which searches a run spends its budget on
│       ├── tables.py          # create/label tables and build their incremental MERGEs
│       └── sources/
│           └── serpapi.py     # FCR discovery through SerpAPI Google search
├── notebooks/
│   ├── 01_ingestion/
│   │   ├── bronze_fcr_documents.py
│   │   └── silver_fcr_documents.py
│   └── 02_processing/
│       └── process_fcr_documents.py
├── resources/
│   └── fcr_pipeline.job.yml
├── tests/
│   ├── fixtures/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_curation.py
│   ├── test_data_processor.py
│   ├── test_download.py
│   ├── test_schemas.py
│   ├── test_search_plan.py
│   ├── test_serpapi_source.py
│   └── test_tables.py
├── .github/
│   ├── workflows/
│   │   └── ci.yml
│   └── CODEOWNERS
├── databricks.yml
├── project_config.yml
├── pyproject.toml
├── uv.lock
├── version.txt
└── README.md
```

**Main components**

- `src/fcr_curator/`: reusable Python package for configuration (including the assistant system prompt), sources, curation rules, downloads, table schemas and MERGEs, and application logic. Notebooks stay lean and import from it; a new source goes in `sources/<name>.py` and returns rows matching `SOURCE_SCHEMA`.
- `notebooks/01_ingestion/`: FCR discovery and curation pipelines.
- `notebooks/02_processing/`: PDF parsing with `ai_parse_document`, chunk cleaning, and embedding (`data_processor.py`).
- `notebooks/03_rag/`: planned retrieval-augmented generation workflows.
- `notebooks/04_evaluation/`: planned model evaluation, tracing, and monitoring using MLflow.
- `resources/`: Databricks Asset Bundle job definitions, kept separate from the notebooks: one scheduled pipeline job.
- `databricks.yml`: deployment targets (`dev`, `acc`, `prd`), bundle variables (`usage_policy_id`, `schedule_pause_status`), and bundle configuration.
- `project_config.yml`: environment-specific catalog, schema, volume (`fcr_documents`), model services, and warehouse settings.
- `pyproject.toml` and `uv.lock`: Python dependencies and reproducible environment management.
- `tests/`: automated tests for configuration, the SerpAPI source, curation rules, downloads, chunking, MERGEs, and schema/row consistency.
- `.github/workflows/ci.yml`: continuous integration for code-quality checks and tests.
- `.github/CODEOWNERS`: pull-request ownership and review requirements.

The earlier BMA consultation paper curator, paused while the BMA website is down, lives on the `bma_consultation_curator` branch.

## 7. Getting Started

### Install dependencies

```bash
uv sync --extra dev --extra ci
```

If using an already activated project virtual environment:

```bash
uv sync --extra dev --extra ci --active
```

### Install pre-commit hooks

```bash
uvx pre-commit install
```

Pre-commit hooks format code and run the configured Ruff checks. If the hooks modify files during a commit, stage the changes and commit again.

### Validate, deploy, and run

Create the SerpAPI secret first (see [SerpAPI key](#serpapi-key)), then:

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
# Free: print the search plan and budget, spending no searches
databricks bundle run fcr_pipeline -t dev --only bronze_fcr_documents --params dry_run=true
# Spend searches and run every stage
databricks bundle run fcr_pipeline -t dev
```

The job's parameters are `env` (defaults to the target) and `dry_run` (defaults to `false`). `--only` runs just the named task.

The silver job downloads every curated FCR not yet in the volume (`DOWNLOAD_FILES = True`, `MAX_DOWNLOADS = None` in `silver_fcr_documents.py`); files already in the volume are skipped on later runs.

The deployment commands require Databricks authentication and the relevant target permissions.

## 8. Environments and CI/CD

The Databricks Asset Bundle supports three targets:

| Environment | Target | Catalog | Access |
|---|---|---|---|
| Development | `dev` | `llmops_dev` | Development and testing |
| Acceptance | `acc` | `llmops_acc` | Validation |
| Production | `prd` | `llmops_prd` | Production workloads |

Each environment has its own MLflow experiment and Unity Catalog tracing destination.

You can manage the development experiment and write development traces. Acceptance and production resources are read-only to the student, while the respective deployment service principals manage their environments.

The GitHub Actions CI workflow runs on pull requests and pushes to `main`, installing dependencies and executing pre-commit checks and tests.

## 9. Course Community

Join the [LLMOps course Discord](https://discord.gg/V4XRYcBFDE) using the Discord username provided during registration.

Course channels become available to participants included in the student list.

---

**Project objective:** Build a governed, evidence-based FCR Curator demonstrating the complete Databricks LLMOps lifecycle, from ingestion and retrieval to generation, evaluation, and monitoring.
