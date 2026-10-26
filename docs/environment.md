# Environment

## Provisioned resources

| Resource | Value |
|---|---|
| Schemas | `llmops_dev.nicholasmisawo`, `llmops_acc.nicholasmisawo`, `llmops_prd.nicholasmisawo` |
| Volume | `fcr_documents` (created by the silver task) |
| SQL warehouse | `0e56acba898dad4a` (`wh-llmops-nicholasmisawo`) |
| Usage policy | `ffe08ae3-a783-3201-ab0c-2fff24538807` |
| MLflow experiments | `/llmops/nicholasmisawo-dev`, `-acc`, `-prd` |
| Lakebase project | `lb-llmops-nicholasmisawo`, shared by every environment (0.5–2 CU, suspends after five idle minutes) |

## Model services (Unity Gateway)

| Service | Model | Used for |
|---|---|---|
| `course_ops.gateway.chat` | GPT-OSS-120B | Query rewriting, reranking |
| `course_ops.gateway.embed` | GTE Large (`gte-large-en-v1.5`, 1,024 dimensions, 8,192-token window) | Chunk and question embeddings |

Both are called through the gateway's OpenAI-compatible API at `<workspace>/ai-gateway/mlflow/v1`
(`gateway.py`). SQL `ai_query` can't reach them; `system.ai.gte-large-en` is the same embedding model
and is reachable from SQL.

## Environments and CI

| Target | Catalog | Access |
|---|---|---|
| `dev` | `llmops_dev` | Yours: development and testing |
| `acc` | `llmops_acc` | Read-only to you; deployed on merge to `main` |
| `prd` | `llmops_prd` | Read-only to you; deployed after approval; the only target whose schedule runs |

GitHub Actions (`.github/workflows/ci.yml`) runs the pre-commit hooks and tests on every pull request
and every push to `main`.
