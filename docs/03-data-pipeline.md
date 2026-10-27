# 03 Data pipeline

Bermuda insurers and reinsurers publish their FCRs on their own websites, so there's no single listing to scrape. The
pipeline finds them with Google search (through SerpAPI), curates the results, and downloads the PDFs.

## The job

One job, `fcr_pipeline-<target>`, runs the stages in order; each starts only when the one before succeeds,
and only one run goes at a time (a second start queues). Python does only what dbt can't: calling
SerpAPI, reading disclosures pages and downloading PDFs. dbt does the rest: the search plan, curation
and parsing included.

| Task | Runs | Writes |
|---|---|---|
| `dbt_search_plan` | dbt (`fcr_search_plan`, a Python model running `search_plan.py`) | The BMA seeds and the entity list (`stg_fcr_entities`), tested; `fcr_search_plan` |
| `search_fcrs` | `01_ingestion/search_fcrs.py` | Appends to `raw_fcr_search_hits`, `raw_fcr_searches`, `raw_fcr_page_reads` |
| `dbt_register` | dbt (`int_fcr_search_results`, then `int_fcr_register`, a Python model running `curation.py`) | `fcr_register` |
| `download_fcrs` | `01_ingestion/download_fcrs.py` | PDFs in the `fcr_documents` volume; appends to `raw_fcr_download_attempts` |
| `dbt_context` | dbt | `fcr_parsed_documents` (`ai_parse_document`), section chunks, embeddings, `fcr_knowledge_base`, `dim_fcr_entities` ([02 Layers](02-layers-and-dbt.md)) |
| `sync_knowledge_base_to_lakebase` | `02_retrieval/sync_knowledge_base_to_lakebase.py` | Lakebase synced table `fcr_knowledge_base_lakebase_<target>` |

It runs weekly, Mondays at 06:00 Bermuda time. The schedule is paused in `dev` and `acc` and active in
`prd` (`schedule_pause_status` in `databricks.yml`). Parameters: `env` (defaults to the target) and
`dry_run` (defaults to `false`). `--only <task>` runs one task, and `--only <task>+` also runs the ones after it.

## Search on a budget

The SerpAPI plan allows 1,000 searches a month, and an empty results page still counts as one. So each
run starts with dbt planning searches only where a new FCR is likely (`fcr_search_plan`, running
`search_plan.py`), and the search task asks them in this order until its budget is spent:

| Priority | Query | Asked when | Pages |
|---|---|---|---|
| 1. `missing_year` | `site:<domain> "financial condition report" <year> filetype:pdf` | A domain in the register lacks the latest reporting year | 1 |
| 2. `institution` | `"<name>" "financial condition report" filetype:pdf`, the name without legal words ("Arch Reinsurance", not "Arch Reinsurance Ltd.") | An insurer on the entity list lacks the latest year; asked under its name and each alias | 1 |
| 3. `discovery` | Three templates for each of the last 3 reporting years: "financial condition report" Bermuda, the same with "Bermuda Monetary Authority", and "solvency and financial condition report" Bermuda | Always; the only way to find new domains | 2 |

The `institution` names come from the entity list ([below](#the-entity-list)), searched **by licence
class** - Class 4, Long Term E, 3B, then groups - and within a class largest group first, so if a run's
budget runs out, the less important wait. An insurer counts as covered only by an FCR registered under
its own name: "Everest Group Ltd." isn't covered by "Everest Reinsurance (Bermuda), Ltd."'s FCR.

- **No repeats**: every search is recorded in `raw_fcr_searches`, and a query waits 28 days before it runs again, or 90 if it found nothing FCR-like.
- **No wasted pages**: a second page is asked only when the first was full and held something FCR-like.
- **Targeted results**: every search sends `gl=bm` (rank as if from Bermuda), `lr=lang_en` and `nfpr=1` (no auto-correction). `cr=countryBM` and date filters are left out: most FCRs are hosted outside Bermuda, and Google's PDF dates are unreliable.
- **Hard caps**: a free account check comes first, then at most `min(150, searches left − 100)` per run, or about 650 a month at most.
- **Dry run**: `--only dbt_search_plan,search_fcrs --params dry_run=true` builds the plan and prints it with its cost, without searching.
- **Nothing lost on failure**: if SerpAPI fails partway, what the run already paid for is appended, then the task fails.

The key comes from the Databricks secret `fcr_curator` / `serpapi_api_key`:

```bash
databricks secrets create-scope fcr_curator --profile <profile>
databricks secrets put-secret fcr_curator serpapi_api_key --profile <profile>
```

## The entity list

The BMA lists every entity it regulates. Its insurers (`stg_bma_insurers`) are the FCRs there should be:
one row per insurer, with its licences, whether it must publish an FCR, the class and business it files
under, and whether it's in liquidation. The **entity list**, **`stg_fcr_entities`**, is the ones in search
scope: Class 4, Long Term E, 3B and groups (`in_search_scope` in `ref_bma_licence_classes`), not in
liquidation. It's what the plan searches for and what every FCR kept is named after; FCRs of anyone
else are left out of the register and the knowledge base. **`dim_fcr_entities`** shows, per listed
insurer, its class, business and the reporting years of its FCRs found (each published the year after);
one with `fcrs_found = 0` is still searched for.

- **Who files**: under the Insurance (Public Disclosure) Rules 2015, Class 3A, 3B and 4 insurers, Class
  C, D and E long-term insurers and insurance groups publish an FCR, on their website; one without a
  website only has to send it on written request, so some FCRs can't be found by search. Captives
  (Class 1, 2, 3), Long Term A and B, special purpose and collateralized insurers, the innovative
  classes and intermediaries don't. `ref_bma_licence_classes` records this, with a search priority
  for each filing class: Class 4, Long Term E, 3B, groups, Long Term D and C, then 3A.
- **Getting it**: the BMA's `robots.txt` blocks automated access, so the pipeline never fetches bma.bm.
  Filter <https://www.bma.bm/regulated-entities> to the Insurance sector in a browser, copy the table
  into `dbt/seeds/raw_list_of_insurance_entities.txt` (git ignores it: it lists contact people), then
  rebuild the seed, which keeps only names (in proper case, plus the BMA's spelling), licences and
  liquidation status:

  ```bash
  uv run python -m fcr_curator.bma_register dbt/seeds/raw_list_of_insurance_entities.txt dbt/seeds/bma_insurance_register.csv
  ```

| Finding (list copied 2026-10-27) | |
|---|---|
| Licensed insurers | 1,396, from 1,467 records: a composite is listed once per licence |
| FCR filers | 380, of which 9 are in provisional liquidation: **371 expected to publish** |
| By most important filing licence | Class 4: 46 · Long Term E: 83 · 3B: 32 · groups: 33 · Long Term D: 4 · Long Term C: 79 · 3A: 94 |
| Composites | 52 hold licences for more than one kind of business, e.g. Class 4 and Long Term C |
| Entity list | **194 insurers**: 46 Class 4, 83 Long Term E, 32 Class 3B, 33 groups |

**What we know beyond the BMA's list** is `dbt/seeds/fcr_entity_details.csv`, keyed by the BMA's
spelling: the domains a group publishes on, aliases (former names such as Watford Re, brands such as
MS Reinsurance), pages that list FCRs, the group's assets, and a display name where capitals hide a
company's own styling (DaVinci, AmTrust, Lloyd's). The 20 companies first searched by brand were folded
into it: 17 map to 37 licensed entities; Validus and Ironshore were acquired, and Fidelis has no
in-scope licence, so they dropped out.

**Telling sister companies apart.** A group's companies share its website, so a domain narrows an FCR
down to the group and its name decides the company; an exact name beats a longer one that starts the
same, so RenaissanceRe Group's own FCR stays the group's. An FCR on a shared domain that names no
company is left out rather than guessed. Off the group's website, the FCR must name the insurer in full
or shortened ("Everest Re"): a longer name is another company, such as PartnerRe's Irish subsidiaries,
whose Solvency II reports on centralbank.ie the first search run had credited to PartnerRe Group. And
a listed insurer has one FCR per year, wherever it's hosted. The BMA lists some groups under their insurer's name ("Nissan
Global Reinsurance, Ltd. (Group)"); an FCR either could have filed goes to the insurer.

| Finding | |
|---|---|
| Register under the entity list | 55 FCRs from 32 listed insurers kept, of the 90 curated before; the rest were companies outside the 194 (UK Solvency II reports among them) |
| First plan | 170 queries due: 1 `site:` query and 169 name queries; a run's 150-search budget leaves the last 20 for the next run |
| First search of the list | 150 searches, 1,305 results, 517 FCR-like; 107 of the searches found something FCR-like |
| After it | **80 FCRs from 41 of the 194 insurers**, one per insurer and year, 77 downloaded; 13,546 chunks from 40 insurers in the knowledge base and Lakebase |
| Coverage by class | Groups 17 of 33 · Class 4 10 of 46 · Long Term E 11 of 83 · Class 3B 3 of 32 |

## Disclosures pages (`sources/pages.py`)

Many companies link every FCR they've published from one page. The details seed's `fcr_pages` column
lists such pages (full URLs; `;` between several), and the search task reads each one on every run, at no
search cost. Every PDF link becomes a search-results row (`source = "pages"`), with the heading it sits under as its
snippet and the readable filename as its title when the link just says "View". Curation then keeps the
FCRs and drops the rest (annual accounts, policy statements).

A page that fails to load is logged and retried next run; the rest of the search task carries on. A PDF a page no
longer links is delisted, like a search result no longer returned. Keep `domains` to hosts only: they
match FCRs to companies, and a path there would never match.

| Page | Finding |
|---|---|
| `https://www.antaresglobal.com/antares-re` | 15 PDF links, 4 of them FCRs (2024, 2021, 2020, 2019); 3 reach the register (2020 or later) |
| `https://www.sompo.com/reinsurance` | Reads fine but links no FCRs (company overviews, modern slavery statements) |

Two curation fixes came from testing this source: documents are de-duplicated **within their kind**, so
an insurer's annual accounts can't push out its FCR for the same year, and **one file is one document**,
so the same PDF found by a search and on a page isn't counted twice.

## Curation (`curation.py`)

dbt's `int_fcr_register` model re-curates all the search results each run:

- **PDFs only**: a `.pdf` URL, or an extensionless one that a `filetype:pdf` query found. Each download is checked for a PDF header; a login or landing page is removed.
- **Excluded sites**: `bma.bm` and `.gov` (with subdomains), in curation and as `-site:` in queries.
- **What it is**: "Financial Condition Report" and "Solvency and Financial Condition Report" (FCR / SFCR) are the same report in Bermuda, so either name in the title, filename or snippet makes a result an `fcr`.
- **Reporting year**: a year beside the report's name wins over others, such as an upload date in a filename. Years before 2016 (when disclosure began) and the current year are ignored.
- **Institution**: the title before the report's name, else the site's name.
- **One per (re)insurer and year**: duplicates on a domain are merged, keeping one that search still returns, then the best-ranked; the others' ids go in `superseded_ids`.
- **Entity name**: each FCR is matched to the entity list, by domain and name ([how](#the-entity-list)), and named after its insurer in `registered_name`. FCRs matching no listed insurer are left out of the register.
- **Kept for the register**: `INCLUDED_DOC_KINDS = {"fcr"}`, reporting on 2020 or later.

## Tables and incremental runs

What Python lands, append-only (`src/fcr_curator/landing.py`), into tables dbt defines and creates (`dbt/models/staging/_sources.yml`):

| Landed | One row per |
|---|---|
| `raw_fcr_search_hits` | FCR PDF a run found, by search or on a disclosures page, as the source returned it |
| `raw_fcr_searches` | Billed SerpAPI search (results page) |
| `raw_fcr_page_reads` | Disclosures page that loaded |
| `raw_fcr_download_attempts` | Attempt to fetch a PDF: downloaded, failed or rejected (not a PDF) |
| The `fcr_documents` volume | Downloaded FCR, at `<source>/<document_id>.pdf` |

Every row carries `landed_at`, one value per run, which orders the runs.

What dbt builds from them for this stage:

| Model | Holds |
|---|---|
| `fcr_search_plan` | The searches due this run, most promising first |
| `int_fcr_search_results` | Every FCR PDF found: the latest copy, every query that found it, `first_seen_at`, `is_listed` |
| `fcr_register` | Curated FCRs, one per company, year and kind, with `volume_path`, `downloaded_at` (empty until downloaded), `download_attempts` and `last_download_outcome` |
| `fcr_parsed_documents` | `ai_parse_document` output per downloaded FCR, parsed once |

Re-runs are safe, and each costs only what's new:

- **Search** only appends. dbt takes each URL as the latest run found it, with every query that ever found it. A URL is delisted once a later run asked again every query that found it (a disclosures page that loaded counts as asked) and none returned it; finding it again lists it again. dbt unit tests pin this rule down (`dbt/models/intermediate/_intermediate.yml`).
- **The register** is rebuilt from all the search results every run, so an FCR that no longer survives curation leaves it (its file stays in the volume).
- **Downloads** fetch only files missing from the volume; the volume is the record of what's downloaded, and dbt reads it. Each attempt is appended, so `fcr_register` shows an FCR that keeps failing.
- **Parsing** is incremental: only FCRs not yet parsed are, and `full_refresh: false` stops even `dbt build --full-refresh` from parsing every PDF again. A parse stays when its FCR leaves the register, so an FCR that comes back isn't parsed twice.

## Findings

| Run | Searches | Result |
|---|---|---|
| First (discovery only) | 11 of a possible 12 | 77 results from 53 domains; 37 FCRs in the register |
| Second (targeted plus discovery) | 28 of a possible 29 | 184 results from 110 domains; **81 FCRs from 58 insurers and reinsurers**, 78 downloaded |

- **Targeting works**: the 11 `site:` searches for known (re)insurers found 41 FCR-like results, nearly 4 per search.
- **Junk queries cost one page**: `"financial condition report" Bermuda 2024` returned 10 off-topic results (a Facebook post, audited accounts). It stopped after one page and now waits 90 days.
- **False positives found by spot checks**, and excluded since: the New York State Comptroller's own "Financial Condition Report" (`.gov`) and pages from `bma.bm`.
- **EU/UK reports got in**: Steamship Mutual, Lloyd's, Vienna Insurance Group and HSBC publish Solvency II SFCRs, which the "both names count" rule admits. A Bermuda check (for example "BSCR" or "Bermuda" in the text) would keep them out; not done yet.
- **Coverage by year**: 2025: 33 · 2024: 26 · 2023: 18 · 2020–2022: 4.
