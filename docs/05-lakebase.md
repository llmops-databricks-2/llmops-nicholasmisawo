# 05 Lakebase

## Embeddings

dbt's `fcr_chunk_embeddings` Python model embeds the section chunks through the Unity Gateway embeddings
API (`gateway.py`), 64 texts a request, and only text not embedded before. SQL `ai_query` can't be used
for this step: it reaches only Databricks-provided `system.ai` model services, not the course's
`course_ops.gateway.embed`.

The service allows each user 200,000 tokens and 60 requests a minute. A larger backlog can only finish
across several minutes, so a batch the gateway rejects with a 429 waits 60 seconds and retries, up to five
tries, and the job takes longer rather than failing. Vectors are saved only when the model finishes, so a
run that still fails keeps none of that run's work.

## The synced table (`lakebase.py`)

The `sync_knowledge_base_to_lakebase` task keeps a Lakebase copy of dbt's `fcr_knowledge_base`:

| Setting | Value |
|---|---|
| Unity Catalog name | `llmops_dev.nicholasmisawo.fcr_knowledge_base_lakebase_dev` (shown as a *foreign* table) |
| Postgres table | `nicholasmisawo.fcr_knowledge_base_lakebase_dev` in database `databricks_postgres` (schema `nicholasmisawo`, not `public`) |
| Columns | `source_key`, `text`, `account_key` (company), `ts` (reporting year end), `citation_url` (the FCR at the cited page), `classification` (the insurer's FCR licence class), `embedding` |
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

**Optional, after the first sync and again whenever the synced table is recreated**: add a full-text
index for keyword and hybrid search. It's on `_dev` only; each environment needs its own. `LakebasePgVectorManager` (`vector_search.py`) creates it on the same
expression `ChunkSearch.keyword()` matches against (`search.TEXT_SEARCH_VECTOR`), which Postgres needs
in order to use it:

```bash
env -u DATABRICKS_WORKSPACE_ID uv run python -c "from databricks.sdk import WorkspaceClient; from fcr_curator.config import load_config; from fcr_curator.vector_search import LakebasePgVectorManager; LakebasePgVectorManager(load_config('project_config.yml'), 'dev', WorkspaceClient(profile='<profile>')).ensure_indexes()"
```

**Connecting from a deployed app**: `connect()` uses the project's primary compute and the current user
by default. Set `LAKEBASE_ENDPOINT` (with `PGHOST` and `PGDATABASE` if needed) to use another compute;
the other two are ignored without it. `DATABRICKS_CLIENT_ID`, when set, is the Postgres user.

## Checking it

- **Lakebase SQL editor**: `SELECT count(*), count(embedding) FROM nicholasmisawo.fcr_knowledge_base_lakebase_dev;`
- **Catalog Explorer**: `llmops_dev` → `nicholasmisawo` → `fcr_knowledge_base_lakebase_dev`, which links to the sync pipeline
- **CLI**: `databricks postgres get-synced-table synced_tables/llmops_dev.nicholasmisawo.fcr_knowledge_base_lakebase_dev` and look for `SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE`

## Findings

- **Synced in full**: `fcr_knowledge_base_lakebase_dev` holds 15,482 chunks from 44 listed insurers, the same as the Delta table, every one with a `vector(1024)` embedding and its licence class, keyed by `source_key`. Citations open the cited page (`…FCR-2024.pdf#page=15`).
- **Changes reach Lakebase as changes**: filling in `classification` updated every row through the Change Data Feed without re-embedding anything, and chunks of FCRs that left the register were deleted the same way.
- **The first copy outlasts the job**: creating a synced table returns before the initial snapshot finishes, so the first copy took about 15 minutes after the job reported success.
- **Needs pgvector**: without the extension the sync fails with `REQUIRED_EXTENSION_NOT_INSTALLED`, hence the one-time setup above.
- **psycopg stays out of jobs**: loading `psycopg`'s compiled library aborts Python on serverless job compute. The sync only needs the Databricks SDK, so `lakebase.py` imports `psycopg` only inside `connect()`, which jobs never call. Anything that queries Postgres (search) runs from a laptop.
- **A synced table accepts an index**: `CREATE INDEX` worked on `fcr_knowledge_base_lakebase_dev`, which is a partitioned table with one partition. So plans show the partition's index, `partition_155655_to_tsvector_idx`, not `fcr_knowledge_base_lakebase_dev_text_gin`, the parent index that created it.
- **The index saves the search, not the ranking**: with it, `solvency or ratio or capital or requirement` takes about 0.6 s unfiltered and 0.2 s limited to 2024. The index finds its 6,610 matches in about 1 ms; the rest goes on reading those rows and running `ts_rank_cd`, so common words stay slower than rare ones.
- **Scans without an index**: measured before it, on other queries, so not a direct comparison. Exact scans are quick: about 0.1 s filtered, 0.2 s for unfiltered vector search, and about 2.6 s for unfiltered keyword search, after the compute wakes from idle (the first query took 37 s).
