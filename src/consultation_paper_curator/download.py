"""Download curated papers into a Unity Catalog volume and record where they landed."""

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from consultation_paper_curator.http_client import download_file


@dataclass(frozen=True)
class StoredFile:
    """Where a paper's file lives in the volume and when it landed there (``processed``)."""

    volume_path: str
    processed: int | None  # yyyyMMddHHmm, matching the course's arxiv_papers table


@dataclass(frozen=True)
class DownloadStats:
    """Outcome counts for one download run."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def paper_path(volume_root: str, paper: dict) -> str:
    """Return a paper's file location: <volume_root>/<source>/<document_id>.<file_type>."""
    return f"{volume_root}/{paper['source']}/{paper['document_id']}.{paper['file_type']}"


def processed_stamp(moment: datetime) -> int:
    """Encode a moment as a yyyyMMddHHmm long, the course's `processed` format."""
    return int(moment.astimezone(UTC).strftime("%Y%m%d%H%M"))


def _stored_file(path: str) -> StoredFile:
    # The file's mtime is when it first landed, so re-runs report the original download time
    return StoredFile(path, processed_stamp(datetime.fromtimestamp(os.path.getmtime(path), UTC)))


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
