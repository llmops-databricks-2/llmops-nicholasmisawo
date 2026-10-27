"""Download curated FCRs into the Unity Catalog volume.

Each file lands at <volume_root>/<source>/<document_id>.<file_type>. Files already there are skipped,
so a re-run only fetches what's missing. The volume is the record of what's downloaded: dbt reads it
(dbt/models/staging/stg_fcr_volume_files.sql). Every attempt is also returned, for the download task to
land as a fact, so FCRs that keep failing show up in the register.
"""

import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple

import requests
from loguru import logger

from fcr_curator.http_client import download_file

# A PDF starts with "%PDF-", possibly after some junk; PDF readers look in the first 1024 bytes
PDF_HEADER = b"%PDF-"
PDF_HEADER_WINDOW = 1024


class DownloadOutcome(StrEnum):
    """What happened to one download attempt; the values match DownloadStats' fields."""

    DOWNLOADED = "downloaded"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DownloadStats:
    """How a download run went."""

    downloaded: int = 0
    skipped: int = 0  # Already in the volume
    failed: int = 0
    rejected: int = 0  # Downloaded, but not a PDF (e.g. a login page), so removed


class DownloadRun(NamedTuple):
    """The outcome of one run of downloads."""

    attempts: list[dict]  # One row per attempt, for raw_fcr_download_attempts (dbt/models/staging/_sources.yml)
    stats: DownloadStats


def document_path(volume_root: str, document: dict) -> str:
    """Return where a document's file goes in the volume."""
    return f"{volume_root}/{document['source']}/{document['document_id']}.{document['file_type']}"


def is_pdf_file(path: str) -> bool:
    """Return True if the file has a PDF header."""
    with open(path, "rb") as file:
        return PDF_HEADER in file.read(PDF_HEADER_WINDOW)


def _fetch(document: dict, path: str, session: requests.Session, download: Callable) -> DownloadOutcome:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not download(session, document["file_url"], path):
        return DownloadOutcome.FAILED
    if document["file_type"] == "pdf" and not is_pdf_file(path):
        logger.warning(f"Not a PDF, removed: {document['file_url']}")
        os.remove(path)
        return DownloadOutcome.REJECTED
    return DownloadOutcome.DOWNLOADED


def download_documents(
    documents: Iterable[dict],
    volume_root: str,
    session: requests.Session,
    max_downloads: int | None = None,
    pause_seconds: float = 1.0,
    download: Callable[[requests.Session, str, str], bool] = download_file,
) -> DownloadRun:
    """Download the documents not yet in the volume; return every attempt and the run's counts.

    max_downloads caps new attempts (whatever their outcome); None means no cap.
    """
    skipped = 0
    attempts: list[dict] = []
    outcomes: Counter[DownloadOutcome] = Counter()

    for document in documents:
        path = document_path(volume_root, document)

        if os.path.exists(path):
            skipped += 1
            continue
        if max_downloads is not None and outcomes.total() >= max_downloads:
            continue
        attempted_at = datetime.now(UTC).isoformat()
        outcome = _fetch(document, path, session, download)
        outcomes[outcome] += 1
        attempts.append(
            {
                "document_id": document["document_id"],
                "file_url": document["file_url"],
                "outcome": outcome.value,
                "attempted_at": attempted_at,
            }
        )
        if outcome == DownloadOutcome.DOWNLOADED:
            time.sleep(pause_seconds)  # Be polite to insurers' websites

    stats = DownloadStats(skipped=skipped, **{outcome.value: count for outcome, count in outcomes.items()})
    return DownloadRun(attempts, stats)
