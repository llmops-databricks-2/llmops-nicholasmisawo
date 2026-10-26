"""Rules for turning raw search results into curated Financial Condition Reports (FCRs).

Search results are noisy: alongside Bermuda FCRs they return EU/UK Solvency and Financial Condition
Reports (SFCRs), press releases and annual reports. These rules classify each result, extract the
institution and reporting year, and keep one document per institution and year.

Pure Python so the rules can be unit tested without Spark; the curation notebook applies them
to the bronze table's rows.
"""

import re
from collections.abc import Iterable
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

# Checked in order: an SFCR title also contains "financial condition report"
SFCR_PATTERN = re.compile(r"\bsolvency (?:and|&) financial condition report\b|\bsfcr\b")
FCR_PATTERN = re.compile(r"\bfinancial condition report\b|\bfcr\b")
# Every value classify_doc_kind can return
DOC_KINDS = ("fcr", "sfcr", "other")

# Bermuda's public disclosure rules took effect for 2016 year-ends, so earlier years are noise
FIRST_REPORTING_YEAR = 2016
YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
# A year next to the report's name, e.g. "FCR 2024" or "2024 Financial Condition Report"
FCR_YEAR_PATTERN = re.compile(
    r"(?:financial condition report|\bfcr)\D{0,30}?\b(20\d{2})\b|\b(20\d{2})\b\D{0,30}?(?:financial condition report|fcr\b)"
)
# Separators and filler left around an institution name once the report's name is cut from a title
TITLE_FILLER = re.compile(r"(?:\bpdf\b|\bfor the year ended\b.*|\b20\d{2}\b|[\s\-–—|:,()]+)+$|^[\s\-–—|:,]+")

DASHES = str.maketrans({"–": "-", "—": "-", "‒": "-", "−": "-"})
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def file_name(url: str) -> str:
    """Return the decoded filename of a URL."""
    return unquote(PurePosixPath(urlparse(url or "").path).name)


def file_type(url: str) -> str:
    """Return the file extension of a document URL without the dot, e.g. 'pdf' or 'xlsx'."""
    return PurePosixPath(file_name(url)).suffix.lower().lstrip(".")


def normalize_title(title: str) -> str:
    """Normalize dashes, quotes and whitespace, and drop a trailing ellipsis Google adds to long titles."""
    text = (title or "").translate(DASHES).translate(QUOTES)
    text = re.sub(r"\s*(?:\.\.\.|…)$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _readable_file_name(url: str) -> str:
    # "FCR_2024-Arch-Re.pdf" -> "fcr 2024 arch re"
    return re.sub(r"[-_.]+", " ", PurePosixPath(file_name(url)).stem).lower()


def classify_doc_kind(title: str, url: str, snippet: str | None = None) -> str:
    """Classify a result from its title and filename, falling back to its snippet."""
    for text in (f"{normalize_title(title)} {_readable_file_name(url)}".lower(), (snippet or "").lower()):
        if SFCR_PATTERN.search(text):
            return "sfcr"
        if FCR_PATTERN.search(text):
            return "fcr"
    return "other"


def _plausible(year: int, today: date) -> bool:
    # An FCR is filed after its year ends, so the latest possible reporting year is last year
    return FIRST_REPORTING_YEAR <= year < today.year


def reporting_year(title: str, url: str, snippet: str | None = None, today: date | None = None) -> int | None:
    """Return the year an FCR reports on, or None when no plausible year is found.

    Title, then filename, then snippet are searched in turn. A year next to the report's name wins
    over any other year in the same text, since filenames often carry an upload date too.
    """
    today = today or date.today()
    for text in (normalize_title(title).lower(), _readable_file_name(url), (snippet or "").lower()):
        beside_name = [int(a or b) for a, b in FCR_YEAR_PATTERN.findall(text)]
        anywhere = [int(year) for year in YEAR_PATTERN.findall(text)]
        for year in (*beside_name, *anywhere):
            if _plausible(year, today):
                return year
    return None


def institution(title: str, site_name: str | None = None) -> str | None:
    """Return the institution an FCR belongs to: the title before the report's name, else the site's name."""
    normalized = normalize_title(title)
    match = re.search(r"(?:solvency (?:and|&) )?financial condition report|\bs?fcr\b", normalized, flags=re.IGNORECASE)
    name = TITLE_FILLER.sub("", normalized[: match.start()]) if match else ""
    return name or site_name or None


def dedupe_key(text: str) -> str:
    """Key that treats names differing only in case, punctuation or spacing as the same."""
    return re.sub(r"[^a-z0-9]+", "", normalize_title(text).lower())


def _found_as_pdf(search_queries: Iterable[str]) -> bool:
    # Google's filetype:pdf returns only PDFs, so such a result is one even when its URL has no extension
    return any("filetype:pdf" in query for query in search_queries)


def curate_document(raw: dict, today: date | None = None) -> dict:
    """Map one bronze row to its curated form (before filtering and deduplication)."""
    url = raw.get("pdf_url")
    title = raw["title"]
    snippet = raw.get("snippet")
    search_queries = list(raw.get("search_queries") or [])

    return {
        "document_id": raw["document_id"],
        "source": raw.get("source"),
        "institution": institution(title, raw.get("site_name")),
        "domain": raw.get("domain"),
        "title": normalize_title(title),
        "doc_kind": classify_doc_kind(title, url, snippet),
        "file_type": file_type(url) or ("pdf" if _found_as_pdf(search_queries) else ""),
        "file_url": url,
        "reporting_year": reporting_year(title, url, snippet, today=today),
        "snippet": snippet,
        "search_queries": search_queries,
        "search_rank": raw.get("search_rank"),
        "is_listed": raw.get("is_listed", True),
        "superseded_ids": [],
        "ingestion_timestamp": raw.get("ingestion_timestamp"),
        "first_seen_at": raw.get("first_seen_at"),
    }


def _survivor_order(document: dict) -> tuple:
    # Still returned by search first, then the best-ranked result, then the earliest found
    return (not document["is_listed"], document["search_rank"] or 999, document["first_seen_at"] or "")


def deduplicate(documents: list[dict]) -> list[dict]:
    """Keep one document per institution and reporting year on each domain.

    The same FCR is often reachable at several URLs (a CDN copy, a re-upload). Documents with no
    institution fall back to their title. The ids of the dropped duplicates are recorded on the
    survivor's ``superseded_ids``.
    """
    groups: dict[tuple, list[dict]] = {}
    for doc in documents:
        name = dedupe_key(doc["institution"] or doc["title"])
        groups.setdefault((doc["domain"], name, doc["reporting_year"]), []).append(doc)

    survivors = []
    for group in groups.values():
        group.sort(key=_survivor_order)
        survivor, *duplicates = group
        survivor["superseded_ids"] = [d["document_id"] for d in duplicates]
        survivors.append(survivor)

    return sorted(survivors, key=lambda d: (-(d["reporting_year"] or 0), d["institution"] or ""))


def curate(raw_documents: list[dict], file_types: tuple[str, ...] = ("pdf",), today: date | None = None) -> list[dict]:
    """Curate bronze rows: normalize, keep only the given file types, then deduplicate.

    A URL with no extension counts as a PDF when a filetype:pdf query found it; the download checks
    the file really is one.
    """
    curated = [curate_document(raw, today=today) for raw in raw_documents]
    return deduplicate([doc for doc in curated if doc["file_type"] in file_types])


def keep_doc_kinds(documents: Iterable[dict], doc_kinds: Iterable[str]) -> list[dict]:
    """Return the documents whose ``doc_kind`` is one of doc_kinds, in their original order.

    Raises ValueError for an empty set (it would empty silver) or an unknown kind (a typo would
    silently drop that kind's documents).
    """
    kept = set(doc_kinds)
    if not kept:
        raise ValueError(f"No doc_kinds to keep; choose from {list(DOC_KINDS)}")
    unknown = kept - set(DOC_KINDS)
    if unknown:
        raise ValueError(f"Unknown doc_kind(s) {sorted(unknown)}; choose from {list(DOC_KINDS)}")
    return [document for document in documents if document["doc_kind"] in kept]


def keep_reported_since(documents: Iterable[dict], min_year: int | None) -> list[dict]:
    """Return the documents reporting on min_year or later, in their original order.

    None keeps every document. Otherwise documents with no ``reporting_year`` are left out, since
    there's no telling whether they meet the cutoff.
    """
    if min_year is None:
        return list(documents)
    return [document for document in documents if document["reporting_year"] and document["reporting_year"] >= min_year]
