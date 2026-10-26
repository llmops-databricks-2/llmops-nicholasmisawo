"""Download curated documents into a Unity Catalog volume and record where they landed."""

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from loguru import logger

from fcr_curator.http_client import download_file


@dataclass(frozen=True)
class StoredFile:
    """Where a document's file lives in the volume and when it landed there (``processed``)."""

    volume_path: str
    processed: int | None  # yyyyMMddHHmm, matching the course's arxiv_papers table


@dataclass(frozen=True)
class DownloadStats:
    """Outcome counts for one download run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    rejected: int = 0  # Downloaded but not a PDF, e.g. a landing or login page; removed again


def document_path(volume_root: str, document: dict) -> str:
    """Return a document's file location: <volume_root>/<source>/<document_id>.<file_type>."""
    return f"{volume_root}/{document['source']}/{document['document_id']}.{document['file_type']}"


# The PDF spec lets a file carry junk before its header, but readers stop looking after 1024 bytes
PDF_HEADER = b"%PDF-"
PDF_HEADER_WINDOW = 1024


def is_pdf_file(path: str) -> bool:
    """Return whether the file at path starts with a PDF header."""
    with open(path, "rb") as file:
        return PDF_HEADER in file.read(PDF_HEADER_WINDOW)


def processed_stamp(moment: datetime) -> int:
    """Encode a moment as a yyyyMMddHHmm long, the course's `processed` format."""
    return int(moment.astimezone(UTC).strftime("%Y%m%d%H%M"))


def _stored_file(path: str) -> StoredFile:
    # The file's mtime is when it first landed, so re-runs report the original download time
    return StoredFile(path, processed_stamp(datetime.fromtimestamp(os.path.getmtime(path), UTC)))


def download_documents(
    documents: Iterable[dict],
    volume_root: str,
    session: requests.Session,
    max_downloads: int | None = None,
    pause_seconds: float = 1.0,
    download: Callable[[requests.Session, str, str], bool] = download_file,
) -> tuple[dict[str, StoredFile], DownloadStats]:
    """Download documents missing from the volume, skipping those already there.

    ``max_downloads`` caps new download attempts (successful, failed or rejected); files already in the
    volume are always reported. A document expected to be a PDF whose file isn't one is removed and
    counted as rejected. Returns the stored file for each ``document_id`` in the volume.
    """
    stored: dict[str, StoredFile] = {}
    downloaded = skipped = failed = rejected = 0

    for document in documents:
        path = document_path(volume_root, document)

        if os.path.exists(path):
            skipped += 1
        elif max_downloads is not None and downloaded + failed + rejected >= max_downloads:
            continue
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not download(session, document["file_url"], path):
                failed += 1
                continue
            if document["file_type"] == "pdf" and not is_pdf_file(path):
                logger.warning(f"Not a PDF, removed: {document['file_url']}")
                os.remove(path)
                rejected += 1
                continue
            downloaded += 1
            time.sleep(pause_seconds)

        stored[document["document_id"]] = _stored_file(path)

    return stored, DownloadStats(downloaded, skipped, failed, rejected)
