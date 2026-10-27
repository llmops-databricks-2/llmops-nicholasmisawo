# 05 Lakebase

## Embeddings

dbt's `fcr_chunk_embeddings` Python model embeds the section chunks through the Unity Gateway embeddings
API (`gateway.py`), 64 texts a request, and only text not embedded before. SQL `ai_query` can't be used
for this step: it reaches only Databricks-provided `system.ai` model services, not the course's
`course_ops.gateway.embed`.

## The synced table (`lakebase.py`)

The `sync_knowledge_base_to_lakebase` task keeps a Lakebase copy of dbt's `fcr_knowledge_base`:

| Setting | Value |
|---|---|
| Unity Catalog name | `llmops_dev.nicholasmisawo.fcr_knowledge_base_lakebase_dev` (shown as a *foreign* table) |
| Postgres table | `nicholasmisawo.fcr_knowledge_base_lakebase_dev` in database `databricks_postgres` (schema `nicholasmisawo`, not `public`) |
| Columns | `source_key`, `text`, `account_key` (company), `ts` (reporting year end), `citation_url` (the FCR at the cited page), `embedding` |
| Primary key | `source_key` |
| `embedding` | pgvector `vector(1024)`, so Postgres searches it directly |
| Scheduling | `TRIGGERED`: the first run creates it, later runs refresh only what changed (Change Data Feed) and wait |

Every environment shares one Lakebase database, so the target is part of the name, and `lakebase` sets
it apart from `fcr_knowledge_base`, the Delta table it copies, which sorts next to it in Catalog
Explorer. A failed sync fails the task.

**One-time setup, already done**: the `vector` column needs the pgvector extension. Install it from your
machine:

```bash
uv run python -c "from databricks.sdk import WorkspaceClient; from fcr_curator.config import load_config; from fcr_curator.lakebase import ensure_vector_extension; ensure_vector_extension(WorkspaceClient(profile='<profile>'), load_config('project_config.yml'))"
```

## Checking it

- **Lakebase SQL editor**: `SELECT count(*), count(embedding) FROM nicholasmisawo.fcr_knowledge_base_lakebase_dev;`
- **Catalog Explorer**: `llmops_dev` → `nicholasmisawo` → `fcr_knowledge_base_lakebase_dev`, which links to the sync pipeline
- **CLI**: `databricks postgres get-synced-table synced_tables/llmops_dev.nicholasmisawo.fcr_knowledge_base_lakebase_dev` and look for `SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE`

## Findings

- **Synced in full**: `fcr_knowledge_base_lakebase_dev` holds 20,768 chunks from 58 companies, every one with a `vector(1024)` embedding, keyed by `source_key`. Citations open the cited page (`…FCR-2024.pdf#page=15`).
- **The first copy outlasts the job**: creating a synced table returns before the initial snapshot finishes, so the first copy took about 15 minutes after the job reported success.
- **Needs pgvector**: without the extension the sync fails with `REQUIRED_EXTENSION_NOT_INSTALLED`, hence the one-time setup above.
- **psycopg stays out of jobs**: loading `psycopg`'s compiled library aborts Python on serverless job compute. The sync only needs the Databricks SDK, so `lakebase.py` imports `psycopg` only inside `connect()`, which jobs never call. Anything that queries Postgres (search) runs from a laptop.
- **No indexes**: the sync owns the table, so it has none. Exact scans are still quick: about 0.1 s filtered, 0.2 s for unfiltered vector search, and about 2.6 s for unfiltered keyword search, after the compute wakes from idle (the first query took 37 s).
