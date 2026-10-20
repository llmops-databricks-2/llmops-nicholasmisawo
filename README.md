# FX Market Intelligence Assistant

**Databricks LLMOps | Central Bank Intelligence & FX Market Analytics**

An AI-powered Market Intelligence Assistant that combines central bank communications with historical foreign exchange data to support monetary policy research and FX market analysis.

Built as part of the **LLMOps with Databricks** course, the project demonstrates end-to-end ingestion, governance, retrieval-augmented generation (RAG), structured analytics, evaluation, and monitoring.

## 1. Project Overview

### User and Problem

The application supports monetary policy analysts, FX risk analysts, financial market researchers, and economists.

It enables users to analyse central bank communications, examine exchange-rate developments, compare monetary policy narratives, and explore relationships between policy announcements and financial market movements.

### Data Sources

| Source | Dataset | Purpose |
|---|---|---|
| Bank for International Settlements (BIS) | ECB and Federal Reserve speeches | Monetary policy communication analysis |
| European Central Bank (ECB) | Daily EUR/USD exchange rates | FX trends, returns, and event-window analysis |
| ECB Economic Bulletin | Economic and monetary policy publications | Macroeconomic context and market intelligence |

**Initial coverage:** 2024–2026, subject to source availability.

### Example Questions

- How have ECB and Federal Reserve monetary policy narratives evolved?
- What factors have influenced EUR/USD exchange-rate movements?
- How did EUR/USD move around major central bank communications?
- What differences exist between ECB and Fed policy outlooks?
- What economic and financial market developments were highlighted in ECB Economic Bulletins?

The assistant combines RAG-based document retrieval with SQL-based financial analysis to generate evidence-grounded responses with source citations.

## 2. Architecture

The solution uses Databricks to integrate structured and unstructured financial information.

**Data ingestion**
- BIS speeches and ECB Economic Bulletins for unstructured text.
- ECB Data Portal API for structured EUR/USD observations.
- Delta tables for dataset storage and metadata management.
- Unity Catalog Volumes for document storage during subsequent processing.

**Retrieval and analytics**
- Document extraction, cleaning, chunking, and embeddings.
- Semantic retrieval of monetary policy communications.
- SQL analysis of exchange rates and historical returns.
- Speech-event analysis combining retrieved text with numerical observations.

**LLMOps**
- Databricks AI Gateway for chat and embedding models.
- MLflow for experiments, evaluation, tracing, and monitoring.
- Databricks Asset Bundles for environment-specific deployment.
- GitHub Actions for CI/CD.

The assistant examines historical associations and does not assume that central bank communications caused observed exchange-rate movements.

## 3. Data Pipelines

Three ingestion jobs are configured through Databricks Asset Bundles.

| Job | Notebook | Unity Catalog table |
|---|---|---|
| `bis_central_bank_speeches` | `01_ingest_bis_central_bank_speeches.py` | `central_bank_speeches` |
| `ecb_fx_rates` | `02_ingest_ecb_fx_rates.py` | `eurusd_exchange_rates` |
| `ecb_economic_bulletins` | `03_ingest_ecb_economic_bulletins.py` | `ecb_economic_bulletins` |

Each job:

- Runs as a serverless Databricks notebook task.
- Installs the packaged project wheel.
- Loads environment-specific settings from `project_config.yml`.
- Writes ingestion results into the corresponding Unity Catalog schema.
- Uses an environment-specific job name to avoid deployment conflicts.

Document-processing fields such as `processed` and `volume_path` are reserved for subsequent pipeline stages.

## 4. Provisioned Databricks Resources

The following infrastructure was provisioned as part of the course.

| Resource | Value |
|---|---|
| Development schema | `llmops_dev.nicholasmisawo` |
| Acceptance schema | `llmops_acc.nicholasmisawo` |
| Production schema | `llmops_prd.nicholasmisawo` |
| Project document volume | `fx_market_intel_files` |
| SQL warehouse ID | `0e56acba898dad4a` |
| Usage policy ID | `ffe08ae3-a783-3201-ab0c-2fff24538807` |
| Dev MLflow experiment | `/llmops/nicholasmisawo-dev` |
| Acc MLflow experiment | `/llmops/nicholasmisawo-acc` |
| Prd MLflow experiment | `/llmops/nicholasmisawo-prd` |
| Chat model service | `course_ops.gateway.chat` |
| Embedding model service | `course_ops.gateway.embed` |

The existing document volume name is retained for compatibility with earlier configurations. It may be renamed to `market_intelligence_documents` in a future migration.

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
            "content": "Explain the relationship between ECB policy and EUR/USD.",
        }
    ],
    reasoning_effort="low",
    max_tokens=1024,
)

embeddings = client.embeddings.create(
    model="course_ops.gateway.embed",
    input="ECB monetary policy and exchange-rate developments",
)
```

The `databricks-openai` package is not currently included in the repository dependencies. The APIs can also be accessed through the OpenAI client configured with Databricks authentication and the `/ai-gateway/mlflow/v1` base URL.

The `embedding_endpoint` configuration identifies an AI Gateway embedding service, not a managed Vector Search embedding endpoint.

## 5. Lakebase and Vector Storage

The `lakebase_instance` in `project_config.yml` identifies the provisioned Lakebase project shared across development, acceptance, and production.

Unity Catalog schemas remain environment-specific.

The course does not automatically create an application database or install `pgvector`. These resources must be configured during the relevant lecture.

Lakebase supports PostgreSQL-based vector storage, while Databricks managed Vector Search would require separate endpoint and index provisioning.

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
│   └── fx_market_intel/
│       ├── __init__.py
│       └── config.py
├── notebooks/
│   ├── 01_ingestion/
│   │   ├── 01_ingest_bis_central_bank_speeches.py
│   │   ├── 02_ingest_ecb_fx_rates.py
│   │   └── 03_ingest_ecb_economic_bulletins.py
│   └── 02_experiments/
│       └── 01_experiment_unity_gateway.py
├── resources/
│   ├── bis_central_bank_speeches.yml
│   ├── ecb_fx_rates.yml
│   └── ecb_economic_bulletins.yml
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

- `src/fx_market_intel/` — Reusable Python package for configuration, data ingestion, analytics, and AI application logic.
- `notebooks/01_ingestion/` — Data ingestion pipelines for BIS speeches, ECB EUR/USD rates, and ECB Economic Bulletins.
- `notebooks/02_experiments/` — Model experimentation using Databricks Unity Gateway, including chat completions, embeddings, token usage, and latency.
- `notebooks/03_processing/` — Planned document extraction, cleaning, chunking, and preparation for embeddings.
- `notebooks/04_rag/` — Planned retrieval-augmented generation and market intelligence workflows.
- `notebooks/05_evaluation/` — Planned model evaluation, tracing, and monitoring using MLflow.
- `resources/` — Databricks Asset Bundle job definitions, separate from notebook implementation.
- `databricks.yml` — Deployment targets (`dev`, `acc`, `prd`) and bundle configuration.
- `project_config.yml` — Environment-specific catalog, schema, volume (`fx_market_intel_files`), model services, and warehouse settings.
- `pyproject.toml` and `uv.lock` — Python dependencies and reproducible environment management.
- `tests/` — Automated tests, including configuration validation.
- `.github/workflows/ci.yml` — Continuous integration for code-quality checks and tests.
- `.github/CODEOWNERS` — Pull-request ownership and review requirements.

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

Pre-commit hooks format code and run configured Ruff checks. If hooks modify files during a commit, stage the changes and commit again.

### Validate the Databricks bundle

```bash
databricks bundle validate -t dev
```

### Deploy ingestion jobs

```bash
databricks bundle deploy -t dev
```

### Run an ingestion job

```bash
databricks bundle run bis_central_bank_speeches -t dev
databricks bundle run ecb_fx_rates -t dev
databricks bundle run ecb_economic_bulletins -t dev
```

The deployment commands require Databricks authentication and the relevant target permissions.

## 8. Environments and CI/CD

The Databricks Asset Bundle supports three targets:

| Environment | Target | Catalog | Access |
|---|---|---|---|
| Development | `dev` | `llmops_dev` | Development and testing |
| Acceptance | `acc` | `llmops_acc` | Validation |
| Production | `prd` | `llmops_prd` | Production workloads |

Each environment has its own MLflow experiment and Unity Catalog tracing destination.

The user can manage the development experiment and write development traces. Acceptance and production resources are read-only to the student, while the respective deployment service principals manage their environments.

The GitHub Actions CI workflow runs on pull requests and pushes to `main`, installing dependencies and executing pre-commit checks and tests.


## 9. Course Community

Join the [LLMOps course Discord](https://discord.gg/V4XRYcBFDE) using the Discord username provided during registration.

Course channels become available to participants included in the student list.

---

**Project objective:** Build a governed, evidence-based Market Intelligence Assistant demonstrating the complete Databricks LLMOps lifecycle—from ingestion and retrieval to financial analytics, generation, evaluation, and monitoring.
