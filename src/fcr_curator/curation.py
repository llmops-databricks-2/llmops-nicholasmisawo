"""Rules that turn raw search results (bronze) into curated FCRs (silver).

Search results are noisy: next to Bermuda FCRs they include annual reports and press releases. In the
Bermuda context a "Financial Condition Report" and a "Solvency and Financial Condition Report" are the
same report, and every search is Bermuda-scoped, so both names count as an FCR. For each result these
rules work out:

- what it is (doc_kind): fcr or other
- whose report it is (institution) and which year it covers (reporting_year)

then keep one document per institution and year. Plain Python, so it can be tested without Spark.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

DOC_KINDS = ("fcr", "other")
# Either name of the report, or its abbreviation: FCR, SFCR, (Solvency and) Financial Condition Report
FCR_NAME_PATTERN = re.compile(r"(?:\bsolvency (?:and|&) )?\bfinancial condition report\b|\bs?fcr\b", re.IGNORECASE)

# Bermuda's disclosure rules started with 2016 year-ends, so an earlier year is noise
FIRST_REPORTING_YEAR = 2016
YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
# A year right next to the report's name, e.g. "FCR 2024", "SFCR 2024" or "2024 Financial Condition Report"
YEAR_BESIDE_NAME_PATTERN = re.compile(
    r"(?:financial condition report|\bs?fcr)\D{0,30}?\b(20\d{2})\b|\b(20\d{2})\b\D{0,30}?(?:financial condition report|fcr\b)"
)
# Leftovers around an institution's name once the report's name is cut out of a title
TITLE_LEFTOVERS = re.compile(r"(?:\bpdf\b|\bfor the year ended\b.*|\b20\d{2}\b|[\s\-–—|:,()]+)+$|^[\s\-–—|:,]+")

# Dropped from an institution name before matching, so "Wilton Re Ltd." still finds wiltonre.bm
LEGAL_SUFFIXES = re.compile(r"\b(?:ltd|limited|inc|plc|llc|sa|se|ag|group)\b\.?", re.IGNORECASE)
# Name keys shorter than this ("re", or "" once "Group" is stripped) would match almost any insurer
MIN_NAME_KEY_CHARS = 4

# Sorts a result Google gave no rank after every ranked one
UNRANKED = float("inf")

# Never curated, whatever search returns; subdomains are excluded too (cdn.bma.bm, osc.ny.gov)
EXCLUDED_DOMAINS = (
    "bma.bm",  # The regulator's site, not an insurer's
    "gov",  # US government sites, e.g. the New York State Comptroller's own "Financial Condition Report"
)

DASHES = str.maketrans({"–": "-", "—": "-", "‒": "-", "−": "-"})
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


# --- Text helpers -----------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Return the title with plain dashes and quotes, single spaces, and no '...' Google added."""
    text = (title or "").translate(DASHES).translate(QUOTES)
    text = re.sub(r"\s*(?:\.\.\.|…)$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_key(text: str) -> str:
    """Return letters and digits only, lower-cased: 'Example Re, Ltd.' and 'example re ltd' match."""
    return re.sub(r"[^a-z0-9]+", "", normalize_title(text).lower())


def institution_key(name: str) -> str:
    """Return a name as lower-case letters and digits, legal suffixes removed: 'Wilton Re Ltd.' -> 'wiltonre'."""
    return dedupe_key(LEGAL_SUFFIXES.sub("", name))


def same_institution(name: str, other: str) -> bool:
    """Return True if two names refer to one insurer: one key starts the other, and neither is too short.

    "Everest Re" matches "Everest Reinsurance (Bermuda) Ltd"; "Argo" doesn't match "Cargo", and a bare
    "Group" matches nothing.
    """
    key, other_key = institution_key(name), institution_key(other)
    if min(len(key), len(other_key)) < MIN_NAME_KEY_CHARS:
        return False
    return key.startswith(other_key) or other_key.startswith(key)


def file_name(url: str) -> str:
    """Return the decoded filename at the end of a URL."""
    return unquote(PurePosixPath(urlparse(url or "").path).name)


def file_type(url: str) -> str:
    """Return the URL's file extension, e.g. 'pdf'; '' if it has none."""
    return PurePosixPath(file_name(url)).suffix.lower().lstrip(".")


def _readable_file_name(url: str) -> str:
    # "FCR_2024-Arch-Re.pdf" -> "fcr 2024 arch re"
    return re.sub(r"[-_.]+", " ", PurePosixPath(file_name(url)).stem).lower()


def on_domain(host: str | None, domain: str) -> bool:
    """Return True if host is domain or one of its subdomains: 'cdn.bma.bm' is on 'bma.bm'."""
    host, domain = (host or "").lower(), domain.lower()
    return host == domain or host.endswith(f".{domain}")


def is_excluded_domain(domain: str | None) -> bool:
    """Return True for an EXCLUDED_DOMAINS domain or any of its subdomains."""
    return any(on_domain(domain, excluded) for excluded in EXCLUDED_DOMAINS)


# --- What a result is -------------------------------------------------------------------------


def classify_doc_kind(title: str, url: str, snippet: str | None = None) -> str:
    """Return fcr if the title, filename or snippet names the report, else other."""
    text = f"{normalize_title(title)} {_readable_file_name(url)} {snippet or ''}"
    return "fcr" if FCR_NAME_PATTERN.search(text) else "other"


def reporting_year(title: str, url: str, snippet: str | None = None, today: date | None = None) -> int | None:
    """Return the year the report covers, or None.

    Looks in the title, then the filename, then the snippet. A year next to the report's name beats
    any other year, because filenames often carry an upload date too. Only years from 2016 to last year
    count: an FCR is published after its year ends.
    """
    this_year = (today or date.today()).year
    for text in (normalize_title(title).lower(), _readable_file_name(url), (snippet or "").lower()):
        beside_name = [int(a or b) for a, b in YEAR_BESIDE_NAME_PATTERN.findall(text)]
        anywhere = [int(year) for year in YEAR_PATTERN.findall(text)]
        for year in beside_name + anywhere:
            if FIRST_REPORTING_YEAR <= year < this_year:
                return year
    return None


def institution(title: str, site_name: str | None = None) -> str | None:
    """Return whose report it is: the title before the report's name, else the website's name."""
    normalized = normalize_title(title)
    match = FCR_NAME_PATTERN.search(normalized)
    name = TITLE_LEFTOVERS.sub("", normalized[: match.start()]) if match else ""
    return name or site_name or None


# --- Curating ---------------------------------------------------------------------------------


# Finds the reference name of a curated document's insurer, or None (see reference.entity_resolver)
EntityResolver = Callable[[dict], str | None]


def _no_entity(document: dict) -> None:
    return None


def curate_document(raw: dict, today: date | None = None, resolve_entity: EntityResolver = _no_entity) -> dict:
    """Turn one bronze row into a silver row (before filtering and de-duplication)."""
    url, title, snippet = raw.get("pdf_url"), raw["title"], raw.get("snippet")
    search_queries = list(raw.get("search_queries") or [])
    # Google's filetype:pdf only returns PDFs, so trust it when the URL has no extension
    found_as_pdf = any("filetype:pdf" in query for query in search_queries)

    document = {
        "document_id": raw["document_id"],
        "source": raw.get("source"),
        "registered_name": None,  # Set below, once the fields it's matched on are known
        "institution": institution(title, raw.get("site_name")),
        "domain": raw.get("domain"),
        "title": normalize_title(title),
        "doc_kind": classify_doc_kind(title, url, snippet),
        "file_type": file_type(url) or ("pdf" if found_as_pdf else ""),
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
    return {**document, "registered_name": resolve_entity(document)}


def _preference(document: dict) -> tuple:
    # Still returned by search first, then the best-ranked, then the first found
    return (not document["is_listed"], document["search_rank"] or UNRANKED, document["first_seen_at"] or "")


def _keep_best(documents: list[dict], group_key: Callable[[dict], tuple]) -> list[dict]:
    # One document per group: the preferred one, recording every id it replaced (including earlier passes')
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for document in documents:
        groups[group_key(document)].append(document)

    kept = []
    for group in groups.values():
        best, *duplicates = sorted(group, key=_preference)
        replaced = [id_ for duplicate in duplicates for id_ in (duplicate["document_id"], *duplicate["superseded_ids"])]
        kept.append({**best, "superseded_ids": [*best["superseded_ids"], *replaced]})
    return kept


def _same_report(document: dict) -> tuple:
    # The kind is part of the key: an insurer's FCR and its annual accounts for one year aren't copies
    return (
        document["domain"],
        dedupe_key(document["institution"] or document["title"]),
        document["reporting_year"],
        document["doc_kind"],
    )


def deduplicate(documents: list[dict]) -> list[dict]:
    """Keep one document per file, then one per (domain, institution, reporting year, kind).

    The same PDF can arrive from several sources (a search and a disclosures page), and the same FCR is
    often online at several URLs. The one kept is: still listed, then the best-ranked, then the first
    found. The others' ids go in its superseded_ids.
    """
    one_per_file = _keep_best(documents, lambda document: (document["file_url"],))
    kept = _keep_best(one_per_file, _same_report)
    return sorted(kept, key=lambda d: (-(d["reporting_year"] or 0), d["institution"] or ""))


def curate(
    raw_documents: list[dict],
    file_types: tuple[str, ...] = ("pdf",),
    today: date | None = None,
    resolve_entity: EntityResolver = _no_entity,
) -> list[dict]:
    """Curate every bronze row: keep PDFs not on an excluded domain, then de-duplicate.

    resolve_entity names each document's insurer as the reference list has it (registered_name).
    """
    curated = [curate_document(raw, today=today, resolve_entity=resolve_entity) for raw in raw_documents]
    return deduplicate([d for d in curated if d["file_type"] in file_types and not is_excluded_domain(d["domain"])])


# --- Filters for silver -----------------------------------------------------------------------


def keep_doc_kinds(documents: Iterable[dict], doc_kinds: Iterable[str]) -> list[dict]:
    """Keep documents of the given kinds. An empty or misspelt kind is an error, not an empty silver."""
    kinds = set(doc_kinds)
    if not kinds or not kinds <= set(DOC_KINDS):
        raise ValueError(f"doc_kinds must be a non-empty subset of {list(DOC_KINDS)}, got {sorted(kinds)}")
    return [d for d in documents if d["doc_kind"] in kinds]


def keep_reported_since(documents: Iterable[dict], min_year: int | None) -> list[dict]:
    """Keep documents reporting on min_year or later (undated ones are dropped). None keeps all."""
    if min_year is None:
        return list(documents)
    return [d for d in documents if d["reporting_year"] and d["reporting_year"] >= min_year]
