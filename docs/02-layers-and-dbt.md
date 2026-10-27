# 02 Layers and dbt

## The layers

A file lands untouched; a parser makes it readable; chunking and embedding make it retrievable; one
schema makes it answerable.

| Layer | Meaning | Where | Tables |
|---|---|---|---|
| **Raw** | Lands untouched | Python, appending to tables dbt defines | `raw_fcr_search_hits`, `raw_fcr_searches`, `raw_fcr_page_reads`, `raw_fcr_download_attempts`; PDFs in the `fcr_documents` volume |
| **Bronze** | Machine-readable | dbt `staging/`, `intermediate/` | `stg_fcr_*` views → `int_fcr_search_results` → `int_fcr_register` (curation) → **`fcr_register`** → `int_fcr_documents` |
| **Silver** | Retrievable | dbt `context/` | `fcr_parsed_documents` (parser output) → `fcr_parsed_hashed` → `chunk_fcr_sections` → `fcr_chunks_meta` |
| **Gold** | Answerable | dbt `context/ai/`, `marts/` | `fcr_chunk_embeddings` → **`fcr_knowledge_base`** (copied to Lakebase as `fcr_knowledge_base_lakebase_<target>`); `dim_fcr_entities` |

Reference data, as dbt seeds (`dbt/seeds/`): **`ref_bma_insurance_register`**, list of insuers and reinsurers the BMA
licenses adopted from the BMA website; **`ref_bma_licence_classes`**, which licences file an FCR and which are searched; and
**`ref_fcr_entity_details`**, what we know beyond the BMA's list (domains, aliases, pages). Together they
make the entity list, `stg_fcr_entities` ([03 Data pipeline](03-data-pipeline.md#the-entity-list)).

**Naming.** Raw, bronze, silver and gold are layers of meaning, not table prefixes. Tables Python lands
start with `raw_` (dbt reads them as sources; the names live in `src/fcr_curator/tables.py`); and job tasks and notebooks are
named for what they do (`dbt_search_plan`, `search_fcrs`, `dbt_register`, `download_fcrs`, `dbt_context`).

**What stays in Python, and why.** dbt rebuilds tables from data; it isn't meant to call the outside
world. Two steps do, so they're notebooks:

- **Search** spends SerpAPI searches from a 1,000-a-month budget and reads a secret. A retried model
  or a `--full-refresh` could spend searches again or wipe history that can't be rebuilt. dbt plans
  the searches (`fcr_search_plan`); the notebook only checks the budget, asks the plan's queries in
  order until it's spent, and reads the disclosures pages.
- **Download** writes PDFs to a volume, not rows to a table.

Both only append what they saw (`landing.py`): every PDF found, search made, page read and download
attempted, each stamped with the run (`landed_at`). They never update a row, so the raw tables are a
complete history, and every rule about the data is dbt: the search plan, the current search results
and delisting, curation, parsing and everything after.

**dbt owns the landed tables too.** Their columns are declared once, in `models/staging/_sources.yml`.
Before every run dbt creates any that are missing (`macros/landed_tables.sql`), so the plan can read
them on a target's first run, before a search has appended anything. The Python tasks append in each
table's own types and fail on a row whose columns don't match, and the unit tests check every source's
rows against the same file.

## The dbt models

```text
dbt/models/
  staging/       stg_fcr_search_hits, stg_fcr_searches, stg_fcr_page_reads,
                 stg_fcr_download_attempts   the landed facts, with the run that landed each  (SQL views)
                 stg_fcr_volume_files    the PDFs in the volume, read by path           (SQL view)
                 stg_bma_insurers        each BMA-licensed insurer: files an FCR? class (SQL view)
                 stg_fcr_entities        the entity list: in-scope insurers, details    (SQL view)
  intermediate/  int_fcr_search_results  every PDF found: latest copy, queries, delisting (SQL view)
                 int_fcr_register        curation, fcr_curator.curation                 (Python)
                 int_fcr_documents       each downloaded FCR with its company            (SQL view)
  context/       fcr_parsed_documents    ai_parse_document, each PDF once               (SQL, incremental)
                 fcr_parsed_hashed       content_hash of each parse (package macro)    (SQL)
                 chunk_fcr_sections      our section chunker, fcr_curator.chunking     (Python)
                 fcr_chunks_meta         each chunk with its FCR's current metadata    (SQL)
    ai/          fcr_chunk_embeddings    course_ops.gateway.embed, through gateway.py  (Python)
                 fcr_chunks_embedded     each chunk with the vector for its text       (SQL)
                 fcr_knowledge_base      the package's knowledge_base serving shape    (SQL)
  marts/         fcr_search_plan         the searches due this run, fcr_curator.search_plan (Python)
                 fcr_register            the register the Python tasks read             (SQL view)
                 dim_fcr_entities        each listed insurer: class, business, FCR years (SQL view)
macros/          fcr_volume.sql          the volume's name, path and file listing
                 landed_tables.sql       creates the landed tables from _sources.yml
```

## Where the dbt_context_engineering package is used, and where it isn't

| Package piece | Used? | Why |
|---|---|---|
| `content_hash` | Yes (`fcr_parsed_hashed`) | Tells changed parses from unchanged ones |
| `knowledge_base` | Yes (`fcr_knowledge_base`) | One serving shape with a first-class `citation_url`, here the FCR opened at the cited page (`#page=N`), and a `classification` slot, here the insurer's FCR licence class; incremental, only new or changed rows. Other metadata stays upstream: join `source_id` to `fcr_chunks_meta.id`, or `account_key` to `stg_fcr_entities.name` |
| `chunk` | No: **our section chunker**, as a Python model | It packs units by token count; ours follows the FCR's sections and measured far better ([04 Chunking](04-chunking.md)) |
| `embed` | No: **a Python model on `course_ops.gateway.embed`** | Its Databricks version is SQL `ai_query`, which can't reach a model service the workspace created |
| `attach_metadata` | No: plain SQL join (`fcr_chunks_meta`) | Its output keeps only the package's chunk columns, which would drop `page_id`, needed for citations |

## Each PDF is parsed once, each text embedded once

`fcr_parsed_documents` is incremental and has `full_refresh: false`: a run parses only FCRs not parsed
yet, and even `dbt build --full-refresh` leaves the table alone, so no PDF is ever paid for twice.

`fcr_chunk_embeddings` is incremental too: only chunks with no vector yet for their id and content hash
go to the gateway. A run over unchanged FCRs makes no embedding calls.

## Running dbt

In the job, `dbt_search_plan` runs first, `dbt_register` after search and `dbt_context` after
downloads, all on the SQL warehouse; the Python models run on serverless with the deployed
`fcr_curator` wheel installed (passed as the `fcr_curator_wheel` var). Locally, from `dbt/` (signs in
through the browser):

```bash
uv sync
uv run dbt deps
uv run dbt build --select +fcr_search_plan --vars '{fcr_curator_wheel: <workspace path of the wheel>}'
uv run dbt build --select +fcr_register --vars '{fcr_curator_wheel: <workspace path of the wheel>}'
uv run dbt build --select int_fcr_documents+ --vars '{fcr_curator_wheel: <workspace path of the wheel>}'
```

dbt-core reports `CustomKeyInConfigDeprecation` for `environment_key`, `environment_dependencies`,
`embedding_endpoint` and `volume_root` on the Python models. The first two are dbt-databricks settings,
read from that place; the others have to be plain config keys, because a Python model sees `meta` as
empty. They stay.
