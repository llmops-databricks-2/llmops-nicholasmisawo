# Consultation Paper Curator

**Databricks LLMOps | Regulatory Consultation Intelligence**

An AI-powered assistant that curates regulatory consultation and discussion papers. It helps compliance and policy teams find what is being proposed, who it affects, and when comments are due.

Built as part of the **LLMOps with Databricks** course, the project demonstrates end-to-end ingestion, governance, retrieval-augmented generation (RAG), evaluation, and monitoring.

## 1. Project Overview

### User and Problem

The application supports compliance officers, regulatory policy teams, risk managers, and legal counsel at regulated financial institutions.

Regulators publish a steady stream of consultation papers, discussion papers, thematic reviews, and stakeholder letters. Each one is a PDF on a filterable website, and proposals often span several related documents over months. Keeping track of open consultations, their deadlines, and how a proposal evolved is manual and easy to miss.

### Data Sources

| Source | Dataset | Purpose |
|---|---|---|
| Bermuda Monetary Authority (BMA) [Documents Centre](https://www.bma.bm/documents-centre/documents-policy-and-guidance) | Discussion/Consultation Papers and Thematic Reviews (current and archived) | Proposed regulatory changes, comment deadlines, sector impact |

**Initial coverage:** every consultation document the BMA site lists, about 600 documents as of October 2026. The source is designed so that other regulators can be added later.

### Example Questions

- Which BMA consultations are currently open, and when do comments close?
- What does the BMA propose in its resolution regime for commercial insurers?
- Which consultations affect Digital Asset Business in the last two years?
- How did the parametric special purpose insurer proposal evolve across its consultation paper and stakeholder letters?
- What thematic reviews has the BMA published, and what did they find?

The assistant combines RAG-based document retrieval with SQL over document metadata to generate evidence-grounded responses with source citations.

## 2. Architecture

**Data ingestion**
- The BMA Documents Centre's filter endpoint is queried once per category, topic, and archive flag. This builds structured metadata: type, categories, topics, upload date, comment deadline, and related documents.
- Delta tables store the document metadata.
- Unity Catalog Volumes store the downloaded source PDFs.

**Retrieval and analytics**
- Document extraction, cleaning, chunking, and embeddings.
- Semantic retrieval over consultation text.
- SQL filtering by topic, sector, date, deadline, and archive status.

**LLMOps**
- Databricks AI Gateway for chat and embedding models.
- MLflow for experiments, evaluation, tracing, and monitoring.
- Databricks Asset Bundles for environment-specific deployment.
- GitHub Actions for CI/CD.

## 3. Data Pipelines

Two jobs, one per medallion layer, so each can be run on its own. They're named after the table they write, so the Jobs UI lists bronze before silver. Both are serverless notebook jobs with the packaged project wheel installed and environment-specific settings from `project_config.yml`:

| Job (UI name) | Notebook | Unity Catalog table | Volume folder |
|---|---|---|---|
| `bronze_consultation_papers-<target>` | `bronze_consultation_papers.py` | `bronze_consultation_papers` | — |
| `silver_consultation_papers-<target>` | `silver_consultation_papers.py` | `silver_consultation_papers` | `consultation_papers/<source>/` (only when `DOWNLOAD_FILES = True`) |

Run bronze at least once before silver, since silver reads the bronze table:

```bash
databricks bundle run bronze_consultation_papers -t dev
databricks bundle run silver_consultation_papers -t dev
```

**Bronze** fetches current and archived documents in the *Regulatory Legislative Documents* type under the *Discussion/Consultation Papers* and *Thematic Review* categories, tags each with the site's category and topic/sector filters, and upserts them into bronze. It downloads nothing.

**Silver** curates all of bronze with the rules in `src/consultation_paper_curator/curation.py` and upserts the result into silver:

- Keeps PDFs only. Spreadsheets (BSCR models, templates, response forms) are dropped.
- Classifies each document as `consultation_paper`, `discussion_paper`, `stakeholder_letter`, `notice`, `guidance`, `legislation` or `other` from its title prefix, falling back to the filename.
- Takes the publication date from the upload timestamp in the CDN filename, which is more reliable than the site's listed date. For re-uploaded files it uses the original timestamp.
- Normalizes titles (dashes, quotes, repeated prefixes) and keeps one document per title, current over archived and then newest, recording the dropped ids in `superseded_ids`.

New papers arrive with `processed` and `volume_path` as `None`, following the course's `arxiv_papers` pattern, until the notebook's download section fills them in. That section is gated by `DOWNLOAD_FILES` (currently `True`): it downloads papers that have no `volume_path` yet into the volume as `<source>/<document_id>.pdf`, and sets `processed` (download time as `yyyyMMddHHmm`) and `volume_path` on those rows. Set it to `False` to end the run after the silver summary; it still prints pending papers by `doc_kind`. `MAX_DOWNLOADS` caps new download attempts per run (currently `None`, so everything pending is downloaded). Silver keeps only the kinds listed in `INCLUDED_DOC_KINDS` (currently `consultation_paper`), so those are the only papers downloaded; papers of other kinds are removed from silver by the next merge, and the run logs how many of each kind were left out. An unknown kind or an empty set fails the run rather than silently dropping papers. Silver also keeps only papers published in `MIN_PUBLISHED_YEAR` or later (currently `2020`; `None` keeps every year). While a year is set, papers with no `published_date` are left out too, since there's no telling whether they meet it. Older papers already in silver are removed by the next merge, and the run logs how many were left out.

### Medallion tables and incremental runs

| Table | Layer | Holds | Written by |
|---|---|---|---|
| `bronze_consultation_papers` | bronze | Every document any source has listed, as it returned them, plus `first_seen_at` and `is_listed` | Bronze job |
| `silver_consultation_papers` | silver | Curated papers: PDFs only, classified, deduplicated, filtered to `INCLUDED_DOC_KINDS` published in `MIN_PUBLISHED_YEAR` or later, plus `processed` and `volume_path` | Silver job |

Each table carries a `COMMENT` and a `quality` table property (`bronze` / `silver`), so its layer shows in Catalog Explorer. Both are updated with `MERGE` on `document_id` rather than overwritten, so runs are incremental and safe to repeat:

- **Bronze** inserts new documents and updates only rows whose content changed; a new run timestamp alone is not a change, so unchanged rows and their `first_seen_at` are left alone. Documents the site stops listing are kept with `is_listed = false` and flip back if they reappear.
- **Silver** is re-curated from all of bronze each run (deduplication needs every document) and upserted the same way. Its `MERGE` never writes `processed` or `volume_path`, so downloads survive re-ingestion. Papers that no longer survive curation, for example superseded by a newer duplicate or left out by the kind or year filter, are removed from silver; any file already downloaded for them stays in the volume.
- **Downloads**, when enabled, fetch only rows with no `volume_path` and skip files already in the volume.

The BMA site has no "changed since" filter, so every bronze run still scrapes the full listing (about four minutes); only the writes are incremental.

Whether a consultation is open is computed at query time (`comment_deadline >= current_date()`) rather than stored, so it never goes stale.

### Bronze table schema: `bronze_consultation_papers`

| Column | Type | Description |
|---|---|---|
| `document_id` | string | `bma_<id>`, the BMA document id |
| `title` | string | Document title |
| `summary` | string | Card text, usually comment instructions |
| `comment_deadline` | string | ISO date parsed from `summary`, if any |
| `uploaded_date` | string | Date listed on the site (ISO) |
| `document_type` | string | `Regulatory Legislative Documents` |
| `categories` | array\<string\> | e.g. `Discussion/Consultation Papers` |
| `topics` | array\<string\> | Topic/sector filters, e.g. `Insurance`, `Digital Asset Business` |
| `is_archived` | boolean | Listed under the site's archive |
| `is_stakeholder_letter` | boolean | Title starts with "Stakeholder Letter" |
| `pdf_url` | string | Direct CDN file URL (a few are `.xlsx`) |
| `view_url` / `download_url` | string | BMA viewer and download links |
| `related_documents` | array\<string\> | CDN URLs from the "Related Document" menu |
| `institution` / `source` | string | `BMA` / `bma` |
| `ingestion_timestamp` | string | Ingest run that last inserted or changed the row (UTC ISO) |
| `first_seen_at` | string | Ingest run that first inserted the row (UTC ISO) |
| `is_listed` | boolean | `false` once the site stops listing the document |

### Silver table schema: `silver_consultation_papers`

| Column | Type | Description |
|---|---|---|
| `document_id` | string | `<source>_<id>`, e.g. `bma_12871` |
| `source` / `institution` | string | `bma` / `BMA` |
| `title` | string | Normalized title |
| `doc_kind` | string | `consultation_paper`, `discussion_paper`, `stakeholder_letter`, `notice`, `guidance`, `legislation` or `other` |
| `file_type` | string | File extension, always `pdf` for now |
| `file_url` | string | Direct CDN file URL |
| `published_date` | date | Original upload date from the CDN filename, else `listed_date` |
| `listed_date` | date | Date listed on the site |
| `file_uploaded_at` | timestamp | Original upload timestamp from the CDN filename |
| `comment_deadline` | date | Deadline parsed from the card text, if any |
| `submission_note` | string | Card text, usually comment instructions (raw `summary`) |
| `submission_emails` | array\<string\> | Email addresses in `submission_note` |
| `topics` | array\<string\> | Topic/sector filters |
| `is_archived` | boolean | Listed under the site's archive |
| `is_listed` | boolean | `false` once the site stops listing the document |
| `related_documents` | array\<string\> | CDN URLs from the "Related Document" menu |
| `superseded_ids` | array\<string\> | Ids of duplicates this row replaced |
| `ingestion_timestamp` | string | Ingest run that last changed the bronze row (UTC ISO) |
| `first_seen_at` | string | Ingest run that first saw the document (UTC ISO) |
| `processed` | long | Download time as `yyyyMMddHHmm`; `None` until downloaded (`DOWNLOAD_FILES = True`) |
| `volume_path` | string | File location in the volume; `None` until downloaded (`DOWNLOAD_FILES = True`) |

## 4. Provisioned Databricks Resources

| Resource | Value |
|---|---|
| Development schema | `llmops_dev.nicholasmisawo` |
| Acceptance schema | `llmops_acc.nicholasmisawo` |
| Production schema | `llmops_prd.nicholasmisawo` |
| Project document volume | `consultation_papers` (created by the silver job when `DOWNLOAD_FILES = True`) |
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
            "content": "Summarise the BMA's proposed resolution regime for commercial insurers.",
        }
    ],
    reasoning_effort="low",
    max_tokens=1024,
)

embeddings = client.embeddings.create(
    model="course_ops.gateway.embed",
    input="BMA consultation on parametric special purpose insurers",
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
│   └── consultation_paper_curator/
│       ├── config.py
│       ├── curation.py        # rules that turn bronze rows into silver papers
│       ├── download.py        # download curated papers into the volume
│       ├── http_client.py     # retrying HTTP session and file download
│       ├── schemas.py         # Spark schemas of the bronze and silver tables
│       ├── tables.py          # create/label tables and build their incremental MERGEs
│       └── sources/
│           └── bma.py         # BMA documents-centre scraper
├── notebooks/
│   └── 01_ingestion/
│       ├── bronze_consultation_papers.py
│       └── silver_consultation_papers.py
├── resources/
│   ├── bronze_consultation_papers.job.yml
│   └── silver_consultation_papers.job.yml
├── tests/
│   ├── fixtures/
│   ├── conftest.py
│   ├── test_bma_source.py
│   ├── test_config.py
│   ├── test_curation.py
│   ├── test_download.py
│   ├── test_schemas.py
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

- `src/consultation_paper_curator/`: reusable Python package for configuration (including the assistant system prompt), source scrapers, curation rules, downloads, table schemas and MERGEs, and application logic. Notebooks stay lean and import from it; a new source goes in `sources/<name>.py` and returns rows matching `SOURCE_SCHEMA`.
- `notebooks/01_ingestion/`: data ingestion and curation pipelines, starting with BMA consultation papers.
- `notebooks/02_processing/`: planned document extraction, cleaning, chunking, and preparation for embeddings.
- `notebooks/03_rag/`: planned retrieval-augmented generation workflows.
- `notebooks/04_evaluation/`: planned model evaluation, tracing, and monitoring using MLflow.
- `resources/`: Databricks Asset Bundle job definitions, kept separate from the notebooks.
- `databricks.yml`: deployment targets (`dev`, `acc`, `prd`) and bundle configuration.
- `project_config.yml`: environment-specific catalog, schema, volume (`consultation_papers`), model services, and warehouse settings.
- `pyproject.toml` and `uv.lock`: Python dependencies and reproducible environment management.
- `tests/`: automated tests for configuration, the BMA card parser, curation rules, downloads, and schema/row consistency.
- `.github/workflows/ci.yml`: continuous integration for code-quality checks and tests.
- `.github/CODEOWNERS`: pull-request ownership and review requirements.

## 7. Getting Started

### Install dependencies

```bash
uv sync --extra dev
```

If using an already activated project virtual environment:

```bash
uv sync --extra dev --active
```

### Install pre-commit hooks

```bash
uvx pre-commit install
```

Pre-commit hooks format code and run the configured Ruff checks. If the hooks modify files during a commit, stage the changes and commit again.

### Validate, deploy, and run

```bash
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run bronze_consultation_papers -t dev
databricks bundle run silver_consultation_papers -t dev
```

A full metadata fetch takes about four minutes. The silver job downloads every curated paper not yet in the volume (`DOWNLOAD_FILES = True`, `MAX_DOWNLOADS = None` in `silver_consultation_papers.py`); files already in the volume are skipped on later runs.

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

**Project objective:** Build a governed, evidence-based Consultation Paper Curator demonstrating the complete Databricks LLMOps lifecycle, from ingestion and retrieval to generation, evaluation, and monitoring.
