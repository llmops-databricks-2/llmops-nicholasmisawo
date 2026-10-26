# 02 Chunking

## Parsing

`ai_parse_document` turns each PDF into typed elements, with tables kept as HTML. It runs in the
workspace and handles every insurer's layout, which is why it was chosen over PyPDF2 (loses tables),
pdfplumber (needs tuning per template) and Unstructured.io (its accurate mode needs system packages
serverless can't install; its hosted API sends documents outside the workspace).

What the parser found across the 78 FCRs:

| Element | Count | Used? |
|---|---|---|
| text | 16,241 | Yes |
| section_header | 15,421 | Strategy 2 only, as chunk headings |
| page_number, page_header, page_footer | 10,916 | No: repeated on every page |
| table | 3,496 | Yes |
| figure | 2,004 | No: charts, descriptions turned off to save cost |
| title | 639 | Strategy 2 only, as headings |
| footnote, caption, signature | 858 | Captions only (strategy 2) |

## Two strategies (`chunking.py`)

1. **Per element** (`gold_fcr_chunks`): one chunk per text or table element.
2. **Per section** (`gold_fcr_chunks_by_section`), like Unstructured's `by_title`:
   - a heading starts a section, and every chunk from it begins with the heading
   - text is packed up to 2,000 characters (about 500 tokens), split at sentence ends
   - a section under 200 characters merges into the next
   - tables get chunks of their own, split by rows with the header row repeated

Every chunk carries its FCR's `institution`, `reporting_year`, `domain`, `title`, `file_url` and `page_id`
(counted from 0, as the parser numbers pages).

## Findings: the comparison

| | Per element | **Per section** |
|---|---|---|
| Chunks | 19,737 | **18,045** |
| Median / average size (characters) | 454 / 782 | **773 / 935** |
| Under 50 characters (noise) | 1,869 | **69** (−96%) |
| Over 2,000 characters | 1,594 | **192** (−88%) |
| Largest | 35,926 | **3,548** |

The section strategy is kept for search. It removed the noise (lone company names such as
"Steamship Mutual", labels such as "(absolute amount)"), cut oversized tables down, and labelled every
table. The same Wilton Re FCR, before and after:

| Per element | Per section |
|---|---|
| `<table><tr><th>Revenue</th>… 244,290 …` | `Income 2023`<br>`<table><tr><th>Revenue</th>… 244,290 …` |

Most of what's still over 2,000 characters (183 of 192) is tables where a single row is already that
long, so splitting by rows can't go further.

## Findings: chunk boundaries (no overlap)

Neither strategy overlaps. Splits fall at sentence ends and between table rows, and every piece repeats
its section heading (and a table's header row), so a continuation still says what it's about.

| Kind | Chunks | Continue the previous chunk's section |
|---|---|---|
| Text | 12,687 | 1,416 (11%) |
| Table | 5,358 | 2,443 (46%): one table split by rows, or several tables in one section |

For about 89% of text chunks overlap would add nothing, while costing duplicate text in the index and
the same passage retrieved twice. In the search test, the sentence "…exceeds the requirements shown
below:" and the table it introduced were separate chunks, and the reranker picked both. If facts that
straddle a split start being missed, add sentence overlap to text splits only, or fetch each hit's
neighbouring chunks at retrieval.

## Findings: is the embedding window big enough?

`gte-large-en-v1.5` turns any text into a 1,024-number vector and accepts up to 8,192 tokens.

| Section chunks | Median | 90th percentile | Largest | Largest, estimated tokens |
|---|---|---|---|---|
| Text (12,687) | 664 chars | 1,786 | 2,129 | about 550 |
| Tables (5,358) | 1,180 chars | 1,967 | 3,548 | about 1,800 at most |

The largest chunk uses at most about a quarter of the window. Under the per-element strategy, the
largest table (35,926 characters, roughly 12–18k tokens) would have been over the limit and cut off.
