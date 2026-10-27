# 01 Data pipeline

Bermuda insurers and reinsurers publish their FCRs on their own websites, so there's no single listing to scrape. The
pipeline finds them with Google search (through SerpAPI), curates the results, and downloads the PDFs.

## The job

One job, `fcr_pipeline-<target>`, runs the stages in order; each starts only when the one before succeeds.
Every task is a serverless notebook running the packaged wheel, with settings from `project_config.yml`.

| Task | Notebook | Writes |
|---|---|---|
| `bronze_fcr_documents` | `01_ingestion/bronze_fcr_documents.py` | `bronze_fcr_documents`, `bronze_fcr_search_log` |
| `silver_fcr_documents` | `01_ingestion/silver_fcr_documents.py` | `silver_fcr_documents`; PDFs in the `fcr_documents` volume |
| `process_fcr_documents` | `02_processing/process_fcr_documents.py` | `silver_parsed_fcr_documents`, `gold_fcr_chunks`, `gold_fcr_chunks_by_section` |
| `sync_fcr_chunks_to_lakebase` | `03_retrieval/sync_fcr_chunks_to_lakebase.py` | Lakebase synced table `fcr_chunks_<target>` |

It runs weekly, Mondays at 06:00 Bermuda time. The schedule is paused in `dev` and `acc` and active in
`prd` (`schedule_pause_status` in `databricks.yml`). Parameters: `env` (defaults to the target) and
`dry_run` (defaults to `false`). `--only <task>` runs one task, and `--only <task>+` also runs the ones after it.

## Search on a budget

The SerpAPI plan allows 1,000 searches a month, and an empty results page still counts as one. Bronze
spends searches only where a new FCR is likely (`search_plan.py`):

| Priority | Query | Asked when | Pages |
|---|---|---|---|
| 1. `missing_year` | `site:<domain> "financial condition report" <year> filetype:pdf` | A domain in silver lacks the latest reporting year | 1 |
| 2. `institution` | `"<name>" "financial condition report" filetype:pdf` | A named (re)insurer (`SearchSettings.institutions`) lacks the latest year | 1 |
| 3. `discovery` | Three templates for each of the last 3 reporting years: "financial condition report" Bermuda, the same with "Bermuda Monetary Authority", and "solvency and financial condition report" Bermuda | Always; the only way to find new domains | 2 |

The `institution` names come from the reference list, the dbt seed **`dbt/seeds/bermuda_reinsurers.csv`** (table `ref_bermuda_reinsurers`, tested by dbt), loaded by
`reference.py` and searched **largest assets first**, so if a run's budget runs out, the smallest wait.
The file is a seed list of the top 20 Bermuda reinsurers; a fuller register (such as the BMA's, with
licence classes) can replace it without code changes. A name is matched to silver after removing legal
suffixes, by prefix, and only if the key is 4 or more characters, so a generic silver name such as "Group"
can't mark every "… Group" (re)insurer as already covered. Several seed names are out of date (Validus,
Watford, Third Point Re, Ironshore and Argo were acquired or merged; Everest Re Group is now Everest
Group), and their searches show how stale names fare.

- **No repeats**: every search is logged in `bronze_fcr_search_log`, and a query waits 28 days before it runs again, or 90 if it found nothing FCR-like.
- **No wasted pages**: a second page is asked only when the first was full and held something FCR-like.
- **Targeted results**: every search sends `gl=bm` (rank as if from Bermuda), `lr=lang_en` and `nfpr=1` (no auto-correction). `cr=countryBM` and date filters are left out: most FCRs are hosted outside Bermuda, and Google's PDF dates are unreliable.
- **Hard caps**: a free account check comes first, then at most `min(150, searches left − 100)` per run, or about 650 a month at most.
- **Dry run**: `--params dry_run=true` prints the plan and its cost without searching.
- **Nothing lost on failure**: if SerpAPI fails partway, what was already found and logged is kept, then the task fails.

The key comes from the Databricks secret `fcr_curator` / `serpapi_api_key`:

```bash
databricks secrets create-scope fcr_curator --profile <profile>
databricks secrets put-secret fcr_curator serpapi_api_key --profile <profile>
```

## Disclosures pages (`sources/pages.py`)

Many companies link every FCR they've published from one page. The reference file's `fcr_pages` column
lists such pages (full URLs; `;` between several), and bronze reads each one on every run, at no search
cost. Every PDF link becomes a bronze row (`source = "pages"`), with the heading it sits under as its
snippet and the readable filename as its title when the link just says "View". Curation then keeps the
FCRs and drops the rest (annual accounts, policy statements).

A page that fails to load is logged and retried next run; the rest of bronze carries on. A PDF a page no
longer links is delisted, like a search result no longer returned. Keep `domains` to hosts only: they
match FCRs to companies, and a path there would never match.

| Page | Finding |
|---|---|
| `https://www.antaresglobal.com/antares-re` | 15 PDF links, 4 of them FCRs (2024, 2021, 2020, 2019); 3 reach silver (2020 or later) |
| `https://www.sompo.com/reinsurance` | Reads fine but links no FCRs (company overviews, modern slavery statements) |

Two curation fixes came from testing this source: documents are de-duplicated **within their kind**, so
an insurer's annual accounts can't push out its FCR for the same year, and **one file is one document**,
so the same PDF found by a search and on a page isn't counted twice.

## Curation (`curation.py`)

Silver re-curates all of bronze each run:

- **PDFs only**: a `.pdf` URL, or an extensionless one that a `filetype:pdf` query found. Each download is checked for a PDF header; a login or landing page is removed.
- **Excluded sites**: `bma.bm` and `.gov` (with subdomains), in curation and as `-site:` in queries.
- **What it is**: "Financial Condition Report" and "Solvency and Financial Condition Report" (FCR / SFCR) are the same report in Bermuda, so either name in the title, filename or snippet makes a result an `fcr`.
- **Reporting year**: a year beside the report's name wins over others, such as an upload date in a filename. Years before 2016 (when disclosure began) and the current year are ignored.
- **Institution**: the title before the report's name, else the site's name.
- **One per (re)insurer and year**: duplicates on a domain are merged, keeping one that search still returns, then the best-ranked; the others' ids go in `superseded_ids`.
- **Entity name**: each FCR is matched to the reference list, first by domain (the optional `domains` column), then by name, and named after it in `registered_name`. FCRs from companies not on the list keep `registered_name` empty, rather than getting a guess.
- **Kept for silver**: `INCLUDED_DOC_KINDS = {"fcr"}`, reporting on 2020 or later.

## Tables and incremental runs

| Table | Layer | Holds |
|---|---|---|
| `bronze_fcr_documents` | bronze | Every search result found, plus `first_seen_at` and `is_listed` |
| `bronze_fcr_search_log` | bronze | One row per search made (append-only) |
| `silver_fcr_documents` | silver | Curated FCRs, plus `processed` and `volume_path` once downloaded |
| `silver_parsed_fcr_documents` | silver | `ai_parse_document` output per FCR, parsed once |
| `gold_fcr_chunks` | gold | Chunking strategy 1: one chunk per element |
| `gold_fcr_chunks_by_section` | gold | Chunking strategy 2, used for search: one chunk per section, with embeddings |

Every table is updated with `MERGE`, never overwritten, so re-runs are safe:

- **Bronze** updates a row only when its content changes or a new query finds it; a new rank alone doesn't count, since Google reorders results. A row is delisted only when every query that ever found it was asked again and none returned it.
- **Silver** never touches `processed` or `volume_path` when re-curating, and drops rows that no longer survive curation (their files stay in the volume).
- **Processing** parses, chunks and embeds only what's new, and deletes parsed rows and chunks of FCRs silver dropped.

## Findings

| Run | Searches | Result |
|---|---|---|
| First (discovery only) | 11 of a possible 12 | 77 results from 53 domains; 37 FCRs in silver |
| Second (targeted plus discovery) | 28 of a possible 29 | 184 results from 110 domains; **81 FCRs from 58 insurers and reinsurers**, 78 downloaded |

- **Targeting works**: the 11 `site:` searches for known (re)insurers found 41 FCR-like results, nearly 4 per search.
- **Junk queries cost one page**: `"financial condition report" Bermuda 2024` returned 10 off-topic results (a Facebook post, audited accounts). It stopped after one page and now waits 90 days.
- **False positives found by spot checks**, and excluded since: the New York State Comptroller's own "Financial Condition Report" (`.gov`) and pages from `bma.bm`.
- **EU/UK reports got in**: Steamship Mutual, Lloyd's, Vienna Insurance Group and HSBC publish Solvency II SFCRs, which the "both names count" rule admits. A Bermuda check (for example "BSCR" or "Bermuda" in the text) would keep them out; not done yet.
- **Coverage by year**: 2025: 33 · 2024: 26 · 2023: 18 · 2020–2022: 4.
