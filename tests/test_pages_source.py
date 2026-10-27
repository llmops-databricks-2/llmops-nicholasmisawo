"""Tests for finding FCR PDFs by reading the disclosures pages in the reference list."""

from types import SimpleNamespace

import requests

from fcr_curator.reference import ReferenceEntity
from fcr_curator.sources.pages import extract_pdf_links, fetch_page_documents, page_query, page_rows

PAGE_URL = "https://www.example-re.com/reinsurance"
PAGE = """
<h2>Financial Condition Reports (FCR)</h2>
<a href="/uploads/Example-Re-FCR-2024.pdf">View</a>
<a href="https://cdn.example-re.com/FCR-2023.PDF?v=2">2023 Financial Condition Report</a>
<a href="/uploads/Example-Re-FCR-2024.pdf#page=2">View</a>
<h2>Annual accounts</h2>
<a href="/uploads/Accounts-2024.pdf">View</a>
<a href="/about">About us</a>
"""
ENTITY = ReferenceEntity("Example Re", domains=("example-re.com",), fcr_pages=(PAGE_URL,))


def test_extract_keeps_each_pdf_link_once_made_absolute() -> None:
    """Relative links resolve against the page, non-PDF links are skipped, and repeats count once."""
    urls = [link.url for link in extract_pdf_links(PAGE, PAGE_URL)]

    assert urls == [
        "https://www.example-re.com/uploads/Example-Re-FCR-2024.pdf",
        "https://cdn.example-re.com/FCR-2023.PDF?v=2",
        "https://www.example-re.com/uploads/Accounts-2024.pdf",
    ]


def test_extract_records_the_heading_each_link_sits_under() -> None:
    """A link carries the most recent heading before it, which says what a bare 'View' link is."""
    headings = [link.heading for link in extract_pdf_links(PAGE, PAGE_URL)]

    assert headings == ["Financial Condition Reports (FCR)"] * 2 + ["Annual accounts"]


def test_rows_title_a_generic_link_after_its_file_and_credit_the_entity() -> None:
    """'View' becomes the readable filename; each row names the entity whose page found it."""
    [first, second, _] = page_rows(extract_pdf_links(PAGE, PAGE_URL), ENTITY, PAGE_URL)

    assert (first["title"], second["title"]) == ("Example Re FCR 2024", "2023 Financial Condition Report")
    assert first["site_name"] == "Example Re"
    assert first["search_queries"] == [page_query(PAGE_URL)]


class FakeSession:
    """Serves PAGE for one URL and fails for every other."""

    def get(self, url: str, timeout: int) -> SimpleNamespace:
        """Return the page, or raise as requests does for an unreachable one."""
        if url != PAGE_URL:
            raise requests.ConnectionError(f"{url} unreachable")
        return SimpleNamespace(text=PAGE, raise_for_status=lambda: None)


def test_a_page_that_fails_is_reported_and_the_rest_still_read() -> None:
    """One moved page doesn't stop the others; it's listed in errors and not counted as read."""
    broken = ReferenceEntity("Broken Re", fcr_pages=("https://broken.example/fcrs",))

    read = fetch_page_documents(FakeSession(), [ENTITY, broken])

    assert [page["query"] for page in read.pages_read] == [page_query(PAGE_URL)]
    assert len(read.errors) == 1 and "broken.example" in read.errors[0]
    assert {row["source"] for row in read.documents} == {"pages"}
    assert len(read.documents) == 3


def test_pdfs_found_and_pages_read_have_exactly_their_landed_columns(landed_columns: dict[str, set[str]]) -> None:
    """Every row the pages source returns lands as is: its columns are the ones dbt defines."""
    read = fetch_page_documents(FakeSession(), [ENTITY])

    assert {frozenset(row) for row in read.documents} == {frozenset(landed_columns["raw_fcr_search_hits"])}
    assert {frozenset(row) for row in read.pages_read} == {frozenset(landed_columns["raw_fcr_page_reads"])}
