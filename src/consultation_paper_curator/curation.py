"""Rules for turning raw scraped document metadata into curated consultation papers.

Pure Python so the rules can be unit tested without Spark; the curation notebook applies them
to the raw table's rows.
"""

import re
from datetime import date, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

# Title prefixes, checked in order; the first match decides the document kind
DOC_KIND_PREFIXES = [
    ("stakeholder letter", "stakeholder_letter"),
    ("stakeholders letter", "stakeholder_letter"),
    ("consultation paper", "consultation_paper"),
    ("namlc consultation paper", "consultation_paper"),
    ("consultation", "consultation_paper"),
    ("discussion paper", "discussion_paper"),
    ("notice", "notice"),
    ("guidance note", "guidance"),
]
# Every value classify_doc_kind can return
DOC_KINDS = (
    "consultation_paper",
    "discussion_paper",
    "stakeholder_letter",
    "notice",
    "guidance",
    "legislation",
    "other",
)
LEGISLATION_PATTERN = re.compile(r"\b(act|acts|rules|bill|regulations|amendments?)\b")

# CDN filenames start with one or more upload timestamps, e.g.
# 2026-01-09-10-55-51-2023-09-22-16-25-47-Amendment-Rules-2024.pdf (re-upload, then original)
URL_TIMESTAMPS_PATTERN = re.compile(r"^((?:\d{4}(?:-\d{2}){5}-)+)")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

DASHES = str.maketrans({"–": "-", "—": "-", "‒": "-", "−": "-"})
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def file_name(url: str) -> str:
    """Return the decoded filename of a URL."""
    return unquote(PurePosixPath(urlparse(url or "").path).name)


def file_type(url: str) -> str:
    """Return the file extension of a document URL without the dot, e.g. 'pdf' or 'xlsx'."""
    return PurePosixPath(file_name(url)).suffix.lower().lstrip(".")


def normalize_title(title: str) -> str:
    """Normalize dashes, quotes and whitespace, and collapse a repeated leading prefix."""
    text = (title or "").translate(DASHES).translate(QUOTES)
    text = text.replace('""', '"')
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1]
    text = re.sub(r"\s+", " ", text).strip()

    # "Discussion Paper - Discussion Paper - X" -> "Discussion Paper - X"
    parts = [part.strip() for part in text.split(" - ")]
    while len(parts) > 2 and parts[0].lower() == parts[1].lower():
        parts.pop(0)
    return " - ".join(parts)


def _kind_from_text(text: str) -> str | None:
    lowered = text.lower()
    for prefix, kind in DOC_KIND_PREFIXES:
        if lowered.startswith(prefix):
            return kind
    return None


def classify_doc_kind(title: str, url: str) -> str:
    """Classify a document from its title prefix, falling back to its filename and keywords."""
    title = normalize_title(title)
    name = URL_TIMESTAMPS_PATTERN.sub("", PurePosixPath(file_name(url)).stem)
    name = re.sub(r"-+", " ", name)

    kind = _kind_from_text(title) or _kind_from_text(name)
    if kind:
        return kind

    lowered = title.lower()
    if "consultation" in lowered:
        return "consultation_paper"
    if "discussion paper" in lowered:
        return "discussion_paper"
    if LEGISLATION_PATTERN.search(lowered):
        return "legislation"
    return "other"


def url_timestamps(url: str) -> list[datetime]:
    """Return the upload timestamps embedded at the start of a CDN filename, newest first."""
    match = URL_TIMESTAMPS_PATTERN.match(file_name(url))
    if not match:
        return []
    stamps = re.findall(r"\d{4}(?:-\d{2}){5}", match.group(1))
    return [datetime.strptime(stamp, "%Y-%m-%d-%H-%M-%S") for stamp in stamps]


def original_upload(url: str) -> datetime | None:
    """Return when the file was first uploaded: the last timestamp in its filename."""
    stamps = url_timestamps(url)
    return stamps[-1] if stamps else None


def extract_emails(text: str | None) -> list[str]:
    """Return the distinct email addresses in text, lower-cased, in order of appearance."""
    return list(dict.fromkeys(e.lower() for e in EMAIL_PATTERN.findall(text or "")))


def parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO date string, returning None when missing or invalid."""
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def dedupe_key(title: str) -> str:
    """Key that treats titles differing only in case, punctuation or spacing as the same."""
    return re.sub(r"[^a-z0-9]+", "", normalize_title(title).lower())


def curate_document(raw: dict) -> dict:
    """Map one raw document row to its curated form (before filtering and deduplication)."""
    url = raw.get("pdf_url") or raw.get("download_url")
    uploaded = original_upload(url)
    listed = parse_iso_date(raw.get("uploaded_date"))

    return {
        "document_id": raw["document_id"],
        "source": raw.get("source"),
        "institution": raw.get("institution"),
        "title": normalize_title(raw["title"]),
        "doc_kind": classify_doc_kind(raw["title"], url),
        "file_type": file_type(url),
        "file_url": url,
        "published_date": uploaded.date() if uploaded else listed,
        "listed_date": listed,
        "file_uploaded_at": uploaded,
        "comment_deadline": parse_iso_date(raw.get("comment_deadline")),
        "submission_note": raw.get("summary"),
        "submission_emails": extract_emails(raw.get("summary")),
        "topics": list(raw.get("topics") or []),
        "is_archived": bool(raw.get("is_archived")),
        "is_listed": raw.get("is_listed", True),
        "related_documents": list(raw.get("related_documents") or []),
        "superseded_ids": [],
        "ingestion_timestamp": raw.get("ingestion_timestamp"),
        "first_seen_at": raw.get("first_seen_at"),
    }


def deduplicate(documents: list[dict]) -> list[dict]:
    """Keep one document per title: current over archived, then the most recently published.

    The ids of the dropped duplicates are recorded on the survivor's ``superseded_ids``.
    """
    groups: dict[tuple, list[dict]] = {}
    for doc in documents:
        groups.setdefault((doc["source"], dedupe_key(doc["title"])), []).append(doc)

    survivors = []
    for group in groups.values():
        group.sort(key=lambda d: (d["is_archived"], -(d["published_date"] or date.min).toordinal()))
        survivor, *duplicates = group
        survivor["superseded_ids"] = [d["document_id"] for d in duplicates]
        survivors.append(survivor)

    return sorted(survivors, key=lambda d: d["published_date"] or date.min, reverse=True)


def curate(raw_documents: list[dict], file_types: tuple[str, ...] = ("pdf",)) -> list[dict]:
    """Curate raw rows: normalize, keep only the given file types, then deduplicate."""
    curated = [curate_document(raw) for raw in raw_documents]
    return deduplicate([doc for doc in curated if doc["file_type"] in file_types])
