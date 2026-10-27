"""Find FCR PDFs by reading the pages that link to them, listed in the reference file's `fcr_pages`.

A company's disclosures page usually links every FCR it has published. Reading it costs no SerpAPI
searches and finds older years that search ranks poorly. Each PDF link becomes a search-results row like a search
result, so curation, downloads and chunking treat both sources the same.

Links are kept only if they point at a PDF; whether a PDF is an FCR (and not, say, annual accounts) is
decided later by curation, from its link text, filename and the heading it sits under.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

import requests
from loguru import logger

from fcr_curator.reference import ReferenceEntity
from fcr_curator.urls import domain, normalize_url, url_document_id

SOURCE = "pages"
PAGE_TIMEOUT_SECONDS = 30
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Link texts that say nothing about the file, so its filename is used as its title instead
GENERIC_LINK_TEXTS = {"", "view", "download", "pdf", "here", "click here", "read more", "open"}


@dataclass(frozen=True)
class PdfLink:
    """A link to a PDF on a page, with its text and the heading it sits under."""

    url: str
    text: str
    heading: str


class PageRead(NamedTuple):
    """The outcome of reading every listed page."""

    documents: list[dict]  # One search-results row per distinct PDF link
    pages_read: list[str]  # Pages that loaded, so their missing links can be delisted
    errors: list[str]  # Pages that didn't load, with why


class _LinkCollector(HTMLParser):
    """Collects every <a href> on a page with its text and the most recent heading before it."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self._heading, self._heading_parts = "", []
        self._in_heading = False
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HEADING_TAGS:
            self._in_heading, self._heading_parts = True, []
        elif tag == "a":
            self._href, self._text_parts = dict(attrs).get("href"), []

    def handle_endtag(self, tag: str) -> None:
        if tag in HEADING_TAGS and self._in_heading:
            self._in_heading = False
            self._heading = " ".join(" ".join(self._heading_parts).split())
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text_parts).split()), self._heading))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_parts.append(data)
        if self._href is not None:
            self._text_parts.append(data)


def is_pdf_url(url: str) -> bool:
    """Return True if the URL's path ends in .pdf, whatever its case or query string."""
    return urlsplit(url).path.lower().endswith(".pdf")


def extract_pdf_links(html: str, page_url: str) -> list[PdfLink]:
    """Return the page's links to PDFs, made absolute, each URL once, in page order."""
    collector = _LinkCollector()
    collector.feed(html)
    links: dict[str, PdfLink] = {}
    for href, text, heading in collector.links:
        url = urljoin(page_url, href)
        if is_pdf_url(url) and normalize_url(url) not in links:
            links[normalize_url(url)] = PdfLink(url, text, heading)
    return list(links.values())


def _title(link: PdfLink) -> str:
    if link.text.lower() not in GENERIC_LINK_TEXTS:
        return link.text
    # "View" says nothing; "Antares-Re-FCR-2024_Final-Version.pdf" says what and when
    return re.sub(r"[-_]+", " ", PurePosixPath(unquote(urlsplit(link.url).path)).stem)


def page_rows(links: Iterable[PdfLink], entity: ReferenceEntity, page_url: str) -> list[dict]:
    """Turn a page's PDF links into search-results rows, credited to the entity whose page it is."""
    return [
        {
            "document_id": url_document_id(link.url, SOURCE),
            "title": _title(link),
            "snippet": link.heading or None,  # The section heading, e.g. "Financial Condition Reports (FCR)"
            "pdf_url": normalize_url(link.url),
            "displayed_link": page_url,
            "site_name": entity.name,
            "domain": domain(link.url),
            "search_queries": [page_query(page_url)],
            "search_rank": None,
        }
        for link in links
    ]


def page_query(page_url: str) -> str:
    """Return how a page is recorded in search_queries, so a row shows which page found it."""
    return f"page:{normalize_url(page_url)}"


def fetch_page_documents(session: requests.Session, entities: Iterable[ReferenceEntity]) -> PageRead:
    """Read every entity's fcr_pages and return one search-results row per PDF link found.

    A page that fails to load is logged and listed in errors, and the others still run: one moved page
    shouldn't stop the rest of the search task.
    """
    run_at = datetime.now(UTC).isoformat()
    rows: dict[str, dict] = {}
    pages_read: list[str] = []
    errors: list[str] = []

    for entity in entities:
        for page_url in entity.fcr_pages:
            try:
                response = session.get(page_url, timeout=PAGE_TIMEOUT_SECONDS)
                response.raise_for_status()
            except requests.RequestException as error:
                logger.warning(f"Couldn't read {page_url} ({entity.name}): {error}")
                errors.append(f"{page_url}: {error}")
                continue
            pages_read.append(page_query(page_url))
            for row in page_rows(extract_pdf_links(response.text, page_url), entity, page_url):
                rows.setdefault(row["document_id"], {**row, "source": SOURCE, "ingestion_timestamp": run_at})

    logger.info(f"Read {len(pages_read)} pages ({len(errors)} failed): {len(rows)} PDF links")
    return PageRead(list(rows.values()), pages_read, errors)
