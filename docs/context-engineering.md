# Context engineering

Every strategy that controls what reaches the model, in the order a document meets them.

| Strategy | Where | Why |
|---|---|---|
| Curated sources | `search_plan.py`, `curation.py` | Only FCRs reach the index: `.gov`, `bma.bm` and non-FCR results are dropped, and one copy is kept per (re)insurer and year |
| Noise removal | `chunking.py` | Page headers, footers, page numbers and signatures never become chunks |
| Contextual chunk headers | `chunking.py` | Every section chunk starts with its section heading, so a bare table still says what it is |
| Size control | `chunking.py` | Chunks of at most about 2,000 characters, tables split by rows with the header row repeated, all well inside the embedding model's 8,192-token window |
| Chunk boundaries, no overlap | `chunking.py` | Splits at sentence ends and table rows, each piece repeating its heading. Overlap would mostly duplicate text and waste the token budget ([why](02-chunking.md#findings-chunk-boundaries-no-overlap)) |
| Entity resolution | `reference.py`, `curation.py` | Each FCR is matched to the reference list (by domain, then name) and named after it (`registered_name`), so `wiltonre.bm` or a bare `Group` becomes a clean company name |
| Metadata enrichment | `data_processor.py` | Each chunk carries `registered_name`, institution, reporting year, page and URL, kept current from silver on every run, for filtering and citation |
| Query rewriting | `query_rewriting.py` | User wording becomes FCR wording, and names and years become filters instead of diluting the embedding |
| Metadata filtering | `search.py` | Narrows search to the (re)insurer and year asked about |
| Hybrid retrieval | `search.py` | Exact-term and semantic matching together |
| Reranking | `reranking.py` | The most useful chunks go first, and unrelated ones are dropped |
| Token budget | `context_budget.py` | The context holds only what fits, best first |
| Citations | `context_budget.py` | Every source is numbered with (re)insurer, year, page and URL, so answers can cite it |
| Grounding rules | `config.py` system prompt | Cite sources, never invent figures, and treat retrieved text as data, not instructions |
