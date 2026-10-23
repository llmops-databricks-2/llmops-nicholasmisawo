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

| Job | Notebook | Unity Catalog table | Volume folder |
|---|---|---|---|
| `bma_consultation_papers` | `01_ingest_bma_consultation_papers.py` | `bma_consultation_papers` | `consultation_curator_files/bma/` |

The job:

- Runs as a serverless Databricks notebook task with the packaged project wheel installed.
- Loads environment-specific settings from `project_config.yml`.
- Fetches current and archived documents in the *Regulatory Legislative Documents* type under the *Discussion/Consultation Papers* and *Thematic Review* categories.
- Tags each document with the site's category and topic/sector filters.
- Downloads each source file to the volume as `<document_id>.<ext>`. It skips files already present and caps the run at `MAX_DOWNLOADS`, which is currently 5 while testing.
- Overwrites the Delta table and reports data-quality checks.

### Table schema: `bma_consultation_papers`

| Column | Type | Description |
|---|---|---|
| `document_id` | string | `bma_<id>`, the BMA document id |
| `title` | string | Document title |
| `summary` | string | Card text, usually comment instructions |
| `comment_deadline` | string | ISO date parsed from `summary`, if any |
| `published` | long | Upload date as `yyyyMMdd` |
| `uploaded_date` | string | Upload date (ISO) |
| `document_type` | string | `Regulatory Legislative Documents` |
| `categories` | array\<string\> | e.g. `Discussion/Consultation Papers` |
| `topics` | array\<string\> | Topic/sector filters, e.g. `Insurance`, `Digital Asset Business` |
| `is_archived` | boolean | Listed under the site's archive |
| `is_stakeholder_letter` | boolean | Title starts with "Stakeholder Letter" |
| `pdf_url` | string | Direct CDN file URL (a few are `.xlsx`) |
| `view_url` / `download_url` | string | BMA viewer and download links |
| `related_documents` | array\<string\> | CDN URLs from the "Related Document" menu |
| `institution` / `source` | string | `BMA` / `bma` |
| `ingestion_timestamp` | string | Run time (UTC ISO) |
| `processed` | long | Download time as `yyyyMMddHHmm` |
| `volume_path` | string | File location in the volume |

## 4. Provisioned Databricks Resources

| Resource | Value |
|---|---|
| Development schema | `llmops_dev.nicholasmisawo` |
| Acceptance schema | `llmops_acc.nicholasmisawo` |
| Production schema | `llmops_prd.nicholasmisawo` |
| Project document volume | `consultation_curator_files` (created by the ingestion job) |
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
│   └── consultation_curator/
│       └── config.py
├── notebooks/
│   └── 01_ingestion/
│       └── 01_ingest_bma_consultation_papers.py
├── resources/
│   └── bma_consultation_papers.job.yml
├── tests/
│   └── test_config.py
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

- `src/consultation_curator/`: reusable Python package for configuration (including the assistant system prompt) and application logic.
- `notebooks/01_ingestion/`: data ingestion pipelines, starting with BMA consultation papers.
- `notebooks/02_processing/`: planned document extraction, cleaning, chunking, and preparation for embeddings.
- `notebooks/03_rag/`: planned retrieval-augmented generation workflows.
- `notebooks/04_evaluation/`: planned model evaluation, tracing, and monitoring using MLflow.
- `resources/`: Databricks Asset Bundle job definitions, kept separate from the notebooks.
- `databricks.yml`: deployment targets (`dev`, `acc`, `prd`) and bundle configuration.
- `project_config.yml`: environment-specific catalog, schema, volume (`consultation_curator_files`), model services, and warehouse settings.
- `pyproject.toml` and `uv.lock`: Python dependencies and reproducible environment management.
- `tests/`: automated tests, including configuration validation.
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
databricks bundle run bma_consultation_papers -t dev
```

A full metadata fetch takes about four minutes. To download every source file, set `MAX_DOWNLOADS = None` in the notebook. Files already in the volume are skipped on later runs.

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
