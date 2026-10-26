"""Tests for downloading curated documents into a volume folder."""

from datetime import UTC, datetime
from pathlib import Path

from fcr_curator.download import download_documents, processed_stamp


def document(document_id: str) -> dict:
    """Build the curated fields download_documents relies on."""
    return {"document_id": document_id, "source": "serpapi", "file_type": "pdf", "file_url": f"https://x/{document_id}"}


def fake_download(failing: set[str] | None = None, content: bytes = b"%PDF-1.7") -> tuple[list[str], object]:
    """Return the list of attempted URLs and a downloader that writes content unless its URL fails."""
    attempted: list[str] = []

    def download(_session: object, url: str, path: str) -> bool:
        attempted.append(url)
        if url in (failing or set()):
            return False
        Path(path).write_bytes(content)
        return True

    return attempted, download


def test_download_documents_writes_files_under_source_folder(tmp_path: Path) -> None:
    """Each document lands at <root>/<source>/<document_id>.<file_type>."""
    _, download = fake_download()

    stored, stats = download_documents([document("serpapi_1")], str(tmp_path), None, pause_seconds=0, download=download)

    assert (tmp_path / "serpapi" / "serpapi_1.pdf").read_bytes() == b"%PDF-1.7"
    assert stored["serpapi_1"].volume_path == str(tmp_path / "serpapi" / "serpapi_1.pdf")
    assert (stats.downloaded, stats.skipped, stats.failed) == (1, 0, 0)


def test_download_documents_skips_files_already_present(tmp_path: Path) -> None:
    """A file already in the volume is reported as stored without being fetched again."""
    (tmp_path / "serpapi").mkdir()
    (tmp_path / "serpapi" / "serpapi_1.pdf").write_bytes(b"old")
    attempted, download = fake_download()

    stored, stats = download_documents([document("serpapi_1")], str(tmp_path), None, pause_seconds=0, download=download)

    assert attempted == []
    assert "serpapi_1" in stored
    assert stats.skipped == 1


def test_download_documents_caps_new_attempts_including_failures(tmp_path: Path) -> None:
    """max_downloads limits attempts, and failed documents are left out of the stored files."""
    attempted, download = fake_download(failing={"https://x/serpapi_1"})
    documents = [document("serpapi_1"), document("serpapi_2"), document("serpapi_3")]

    stored, stats = download_documents(
        documents, str(tmp_path), None, max_downloads=2, pause_seconds=0, download=download
    )

    assert attempted == ["https://x/serpapi_1", "https://x/serpapi_2"]
    assert list(stored) == ["serpapi_2"]
    assert (stats.downloaded, stats.failed) == (1, 1)


def test_download_documents_removes_a_file_that_is_not_a_pdf(tmp_path: Path) -> None:
    """A landing or login page saved in place of a PDF is removed, not stored, and counted as rejected."""
    _, download = fake_download(content=b"<!doctype html><html>Sign in</html>")

    stored, stats = download_documents([document("serpapi_1")], str(tmp_path), None, pause_seconds=0, download=download)

    assert stored == {}
    assert not (tmp_path / "serpapi" / "serpapi_1.pdf").exists()
    assert (stats.downloaded, stats.rejected) == (0, 1)


def test_processed_stamp_is_utc_yyyymmddhhmm() -> None:
    """The processed column uses the course's yyyyMMddHHmm long format."""
    assert processed_stamp(datetime(2026, 10, 23, 1, 22, 43, tzinfo=UTC)) == 202610230122
