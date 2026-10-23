"""Tests for downloading curated papers into a volume folder."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from consultation_paper_curator.download import download_papers, processed_stamp, select_for_download


def paper(document_id: str) -> dict:
    """Build the curated fields download_papers relies on."""
    return {"document_id": document_id, "source": "bma", "file_type": "pdf", "file_url": f"https://x/{document_id}"}


def fake_download(failing: set[str] | None = None) -> tuple[list[str], object]:
    """Return the list of attempted URLs and a downloader that writes a file unless its URL fails."""
    attempted: list[str] = []

    def download(_session: object, url: str, path: str) -> bool:
        attempted.append(url)
        if url in (failing or set()):
            return False
        Path(path).write_bytes(b"%PDF")
        return True

    return attempted, download


def test_download_papers_writes_files_under_source_folder(tmp_path: Path) -> None:
    """Each paper lands at <root>/<source>/<document_id>.<file_type>."""
    _, download = fake_download()

    stored, stats = download_papers([paper("bma_1")], str(tmp_path), None, pause_seconds=0, download=download)

    assert (tmp_path / "bma" / "bma_1.pdf").read_bytes() == b"%PDF"
    assert stored["bma_1"].volume_path == str(tmp_path / "bma" / "bma_1.pdf")
    assert (stats.downloaded, stats.skipped, stats.failed) == (1, 0, 0)


def test_download_papers_skips_files_already_present(tmp_path: Path) -> None:
    """A file already in the volume is reported as stored without being fetched again."""
    (tmp_path / "bma").mkdir()
    (tmp_path / "bma" / "bma_1.pdf").write_bytes(b"old")
    attempted, download = fake_download()

    stored, stats = download_papers([paper("bma_1")], str(tmp_path), None, pause_seconds=0, download=download)

    assert attempted == []
    assert "bma_1" in stored
    assert stats.skipped == 1


def test_download_papers_caps_new_attempts_including_failures(tmp_path: Path) -> None:
    """max_downloads limits attempts, and failed papers are left out of the stored files."""
    attempted, download = fake_download(failing={"https://x/bma_1"})
    papers = [paper("bma_1"), paper("bma_2"), paper("bma_3")]

    stored, stats = download_papers(papers, str(tmp_path), None, max_downloads=2, pause_seconds=0, download=download)

    assert attempted == ["https://x/bma_1", "https://x/bma_2"]
    assert list(stored) == ["bma_2"]
    assert (stats.downloaded, stats.failed) == (1, 1)


def test_processed_stamp_is_utc_yyyymmddhhmm() -> None:
    """The processed column uses the course's yyyyMMddHHmm long format."""
    assert processed_stamp(datetime(2026, 10, 23, 1, 22, 43, tzinfo=UTC)) == 202610230122


def test_select_for_download_leaves_out_excluded_doc_kinds() -> None:
    """Papers of an excluded kind are not selected; the others keep their order."""
    papers = [
        {**paper("bma_1"), "doc_kind": "consultation_paper"},
        {**paper("bma_2"), "doc_kind": "notice"},
        {**paper("bma_3"), "doc_kind": "stakeholder_letter"},
    ]

    selected = select_for_download(papers, {"notice"})

    assert [p["document_id"] for p in selected] == ["bma_1", "bma_3"]


def test_select_for_download_without_exclusions_keeps_every_paper() -> None:
    """With nothing excluded, every pending paper is selected."""
    papers = [{**paper("bma_1"), "doc_kind": "notice"}]

    assert select_for_download(papers) == papers


def test_select_for_download_rejects_unknown_doc_kind() -> None:
    """A misspelt kind fails loudly instead of silently excluding nothing."""
    with pytest.raises(ValueError, match="notices"):
        select_for_download([], {"notices"})
