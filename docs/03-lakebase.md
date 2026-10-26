# 03 Lakebase

## Embeddings

The processing task embeds the section chunks through the Unity Gateway embeddings API (`gateway.py`),
64 texts a request, saving every 2,000 so a failure keeps finished work. SQL `ai_query` can't be used
for this step: it reaches only Databricks-provided `system.ai` model services, not the course's
`course_ops.gateway.embed`. The per-element chunks aren't embedded.

## The synced table (`lakebase.py`)

The `sync_fcr_chunks_to_lakebase` task keeps a Lakebase copy of `gold_fcr_chunks_by_section`:

| Setting | Value |
|---|---|
| Unity Catalog name | `llmops_dev.nicholasmisawo.fcr_chunks_dev` (shown as a *foreign* table) |
| Postgres table | `nicholasmisawo.fcr_chunks_dev` in database `databricks_postgres` (schema `nicholasmisawo`, not `public`) |
| Primary key | `id` |
| `embedding` | pgvector `vector(1024)`, so Postgres searches it directly |
| Scheduling | `TRIGGERED`: the first run creates it, later runs refresh only what changed (Change Data Feed) and wait |

Every environment shares one Lakebase database, so the target is part of the name. A failed sync fails
the task.

**One-time setup, already done**: the `vector` column needs the pgvector extension. Install it from your
machine:

```bash
uv run python -c "from databricks.sdk import WorkspaceClient; from fcr_curator.config import load_config; from fcr_curator.lakebase import ensure_vector_extension; ensure_vector_extension(WorkspaceClient(profile='<profile>'), load_config('project_config.yml'))"
```

## Checking it

- **Lakebase SQL editor**: `SELECT count(*), count(embedding) FROM nicholasmisawo.fcr_chunks_dev;`
- **Catalog Explorer**: `llmops_dev` → `nicholasmisawo` → `fcr_chunks_dev`, which links to the sync pipeline
- **CLI**: `databricks postgres get-synced-table synced_tables/llmops_dev.nicholasmisawo.fcr_chunks_dev` and look for `SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE`

## Findings

- **Synced in full**: 18,045 chunks from 78 FCRs, every one with a `vector(1024)` embedding, keyed by `id`.
- **First failure, missing extension**: the sync failed with `REQUIRED_EXTENSION_NOT_INSTALLED` until pgvector was installed.
- **Second failure, a driver crash**: loading `psycopg`'s compiled library aborts Python on serverless job compute. The sync only needs the Databricks SDK, so `lakebase.py` imports `psycopg` only inside `connect()`, which jobs never call. Anything that queries Postgres (search) runs from a laptop.
- **No indexes**: the sync owns the table, so it has none. Exact scans are still quick: about 0.1 s filtered, 0.2 s for unfiltered vector search, and about 2.6 s for unfiltered keyword search, after the compute wakes from idle (the first query took 37 s).
