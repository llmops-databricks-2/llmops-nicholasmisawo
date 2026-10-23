"""Download curated papers into a Unity Catalog volume."""

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from consultation_paper_curator.http_client import download_file


@dataclass(frozen=True)
class StoredFile:
    """Where a paper's file lives in the volume and when it landed there."""

    volume_path: str
    downloaded_at: datetime


@dataclass(frozen=True)
class DownloadStats:
    """Outcome counts for one download run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def paper_path(volume_root: str, paper: dict) -> str:
    """Return a paper's file location: <volume_root>/<source>/<document_id>.<file_type>."""
    return f"{volume_root}/{paper['source']}/{paper['document_id']}.{paper['file_type']}"


def _stored_file(path: str) -> StoredFile:
    # The file's mtime is when it first landed, so re-runs report the original download time
    return StoredFile(path, datetime.fromtimestamp(os.path.getmtime(path), UTC))


def download_papers(
    papers: Iterable[dict],
    volume_root: str,
    session: requests.Session,
    max_downloads: int | None = None,
    pause_seconds: float = 1.0,
    download: Callable[[requests.Session, str, str], bool] = download_file,
) -> tuple[dict[str, StoredFile], DownloadStats]:
    """Download papers missing from the volume, skipping those already there.

    ``max_downloads`` caps new download attempts (successful or failed); files already in the
    volume are always reported. Returns the stored file for each ``document_id`` in the volume.
    """
    stored: dict[str, StoredFile] = {}
    downloaded = skipped = failed = 0

    for paper in papers:
        path = paper_path(volume_root, paper)

        if os.path.exists(path):
            skipped += 1
        elif max_downloads is not None and downloaded + failed >= max_downloads:
            continue
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not download(session, paper["file_url"], path):
                failed += 1
                continue
            downloaded += 1
            time.sleep(pause_seconds)

        stored[paper["document_id"]] = _stored_file(path)

    return stored, DownloadStats(downloaded, skipped, failed)


def with_storage(papers: Iterable[dict], stored: dict[str, StoredFile]) -> list[dict]:
    """Return copies of papers with ``downloaded_at`` and ``volume_path`` set (None if not stored)."""
    rows = []
    for paper in papers:
        stored_file = stored.get(paper["document_id"])
        rows.append(
            {
                **paper,
                "downloaded_at": stored_file.downloaded_at if stored_file else None,
                "volume_path": stored_file.volume_path if stored_file else None,
            }
        )
    return rows
