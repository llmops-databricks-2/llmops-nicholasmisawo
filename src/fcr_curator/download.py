"""Download curated FCRs into the Unity Catalog volume.

Each file lands at <volume_root>/<source>/<document_id>.<file_type>. Files already there are skipped,
so a re-run only fetches what's missing.
"""

import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from loguru import logger

from fcr_curator.http_client import download_file

# A PDF starts with "%PDF-", possibly after some junk; PDF readers look in the first 1024 bytes
PDF_HEADER = b"%PDF-"
PDF_HEADER_WINDOW = 1024


@dataclass(frozen=True)
class StoredFile:
    """Where a file is in the volume, and when it got there."""

    volume_path: str
    processed: int | None  # Download time as yyyyMMddHHmm, the course's format


@dataclass(frozen=True)
class DownloadStats:
    """How a download run went."""

    downloaded: int = 0
    skipped: int = 0  # Already in the volume
    failed: int = 0
    rejected: int = 0  # Downloaded, but not a PDF (e.g. a login page), so removed


def document_path(volume_root: str, document: dict) -> str:
    """Return where a document's file goes in the volume."""
    return f"{volume_root}/{document['source']}/{document['document_id']}.{document['file_type']}"


def processed_stamp(moment: datetime) -> int:
    """Return a moment as a yyyyMMddHHmm number in UTC, e.g. 202610260122."""
    return int(moment.astimezone(UTC).strftime("%Y%m%d%H%M"))


def is_pdf_file(path: str) -> bool:
    """Return True if the file has a PDF header."""
    with open(path, "rb") as file:
        return PDF_HEADER in file.read(PDF_HEADER_WINDOW)


def _stored_file(path: str) -> StoredFile:
    # The file's modified time is when it first arrived, so re-runs keep the original download time
    return StoredFile(path, processed_stamp(datetime.fromtimestamp(os.path.getmtime(path), UTC)))


def _fetch(document: dict, path: str, session: requests.Session, download: Callable) -> str:
    """Download one document; return 'downloaded', 'failed' or 'rejected'."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not download(session, document["file_url"], path):
        return "failed"
    if document["file_type"] == "pdf" and not is_pdf_file(path):
        logger.warning(f"Not a PDF, removed: {document['file_url']}")
        os.remove(path)
        return "rejected"
    return "downloaded"


def download_documents(
    documents: Iterable[dict],
    volume_root: str,
    session: requests.Session,
    max_downloads: int | None = None,
    pause_seconds: float = 1.0,
    download: Callable[[requests.Session, str, str], bool] = download_file,
) -> tuple[dict[str, StoredFile], DownloadStats]:
    """Download the documents not yet in the volume.

    max_downloads caps new attempts (whatever their outcome); None means no cap. Returns the stored file
    of every document now in the volume, by document_id, and the run's counts.
    """
    stored: dict[str, StoredFile] = {}
    counts: Counter[str] = Counter()

    for document in documents:
        path = document_path(volume_root, document)

        if os.path.exists(path):
            counts["skipped"] += 1
        else:
            attempts = counts["downloaded"] + counts["failed"] + counts["rejected"]
            if max_downloads is not None and attempts >= max_downloads:
                continue
            outcome = _fetch(document, path, session, download)
            counts[outcome] += 1
            if outcome != "downloaded":
                continue
            time.sleep(pause_seconds)  # Be polite to insurers' websites

        stored[document["document_id"]] = _stored_file(path)

    return stored, DownloadStats(**counts)
