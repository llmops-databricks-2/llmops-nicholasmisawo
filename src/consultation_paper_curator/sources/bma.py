"""Scraper for Bermuda Monetary Authority (BMA) consultation documents.

Source: https://www.bma.bm/documents-centre/documents-policy-and-guidance
The page's filter panel posts to a `fetchdata` endpoint that returns rendered HTML cards. Cards
carry no type/category/topic labels, so documents are tagged by querying once per filter value.
"""

import re
import time
from datetime import UTC, date, datetime

import requests
from bs4 import BeautifulSoup
from loguru import logger

BMA_PAGE = "https://www.bma.bm/documents-centre/documents-policy-and-guidance"
BMA_FETCH = f"{BMA_PAGE}/documents-policy-and-guidance/fetchdata"
BMA_CDN = "https://cdn.bma.bm/"

DOCUMENT_TYPE = ("9", "Regulatory Legislative Documents")
CATEGORIES = {"14": "Discussion/Consultation Papers", "103": "Thematic Review"}
PAGE_SIZE = 50
REQUEST_TIMEOUT_SECONDS = 60
PAGE_PAUSE_SECONDS = 0.5

DATE_PATTERN = re.compile(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b")
TOPIC_INPUTS = ["parentsector[]", "childsector[]", "subchildsector[]"]


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_date(text: str | None) -> date | None:
    """Parse the first 'DD Month YYYY' date in text into a date."""
    match = DATE_PATTERN.search(text or "")
    if not match:
        return None

    try:
        return datetime.strptime(match.group(), "%d %B %Y").date()
    except ValueError:
        return None


def get_csrf_token(session: requests.Session) -> tuple[str, dict[str, str]]:
    """Open the documents page to get a CSRF token and the topic id -> label map."""
    response = session.get(BMA_PAGE, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    token = soup.find("meta", attrs={"name": "csrf-token"})["content"]

    topics = {}
    for name in TOPIC_INPUTS:
        for checkbox in soup.find_all("input", attrs={"name": name}):
            label = checkbox.find_parent("label") or checkbox.find_next("label")
            if label:
                topics[checkbox["value"]] = _clean_text(label.get_text(" ", strip=True))

    return token, topics


def fetch_page(
    session: requests.Session,
    token: str,
    categories: list[str],
    topics: list[str] | None = None,
    archive: bool = False,
    page: int = 1,
) -> tuple[str, int]:
    """Fetch one page of filtered results; return its HTML fragment and the total match count."""
    response = session.post(
        BMA_FETCH,
        data={
            "limit": PAGE_SIZE,
            "pageNumber": page,
            "types[]": [DOCUMENT_TYPE[0]],
            "sectors[]": list(categories),  # the site sends categories as `sectors`
            "topic[]": list(topics or []),
            "archive": "Y" if archive else "N",
            "isParentsectorClicked": "false",
            "sortDirection": "desc",
        },
        headers={"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("html") or "", int(payload.get("totalcount") or 0)


def parse_cards(html: str) -> list[dict]:
    """Parse document cards from a fetchdata HTML fragment, skipping cards without a title or id."""
    soup = BeautifulSoup(html, "html.parser")
    documents = []

    for card in soup.select("li.document-column"):
        title_link = card.select_one("h3.d-title a[href]")
        id_link = card.select_one("a[data-viewid]")

        if title_link is None or id_link is None:
            continue

        pdf_url = title_link["href"]
        view_link = card.select_one('a[data-viewtype="view"]')
        download_link = card.select_one('a[data-viewtype="download"]')
        summary_tag = card.select_one("div.d-desc > p")
        date_tag = card.select_one("div.date")

        summary = summary_tag.get_text(" ", strip=True) if summary_tag else None
        uploaded = parse_date(date_tag.get_text(" ", strip=True) if date_tag else None)
        deadline = parse_date(summary)
        title = _clean_text(title_link.get_text(" ", strip=True))

        related = [link["href"] for link in card.select(f'a[href^="{BMA_CDN}"]') if link["href"] != pdf_url]

        documents.append(
            {
                "document_id": f"bma_{id_link['data-viewid']}",
                "title": title,
                "summary": summary,
                "comment_deadline": deadline.isoformat() if deadline else None,
                "uploaded_date": uploaded.isoformat() if uploaded else None,
                "document_type": DOCUMENT_TYPE[1],
                "is_stakeholder_letter": title.lower().startswith("stakeholder letter"),
                "pdf_url": pdf_url,
                "view_url": view_link["href"] if view_link else None,
                "download_url": download_link["href"] if download_link else None,
                "related_documents": list(dict.fromkeys(related)),
            }
        )

    return documents


def fetch_all(
    session: requests.Session,
    token: str,
    categories: list[str],
    topics: list[str] | None = None,
    archive: bool = False,
) -> list[dict]:
    """Page through every result for one filter combination."""
    documents = []
    page = 1

    while True:
        html, total = fetch_page(session, token, categories, topics, archive, page)
        batch = parse_cards(html)
        documents.extend(batch)

        if not batch or len(documents) >= total:
            return documents

        page += 1
        time.sleep(PAGE_PAUSE_SECONDS)


def fetch_bma_documents(
    session: requests.Session,
    categories: dict[str, str] = CATEGORIES,
    include_archive: bool = True,
    max_results: int | None = None,
) -> list[dict]:
    """Fetch BMA consultation documents tagged with category, topic and archive status.

    Returns rows matching ``schemas.SOURCE_SCHEMA``, newest first, capped at ``max_results``.
    """
    documents: dict[str, dict] = {}
    archive_flags = [False, True] if include_archive else [False]
    token, topic_labels = get_csrf_token(session)

    # Base pass: one query per category and archive flag
    for category_id, category_name in categories.items():
        for archived in archive_flags:
            for doc in fetch_all(session, token, [category_id], archive=archived):
                record = documents.setdefault(
                    doc["document_id"],
                    {**doc, "categories": [], "topics": [], "is_archived": archived},
                )
                if category_name not in record["categories"]:
                    record["categories"].append(category_name)

            logger.info(f"Category '{category_name}' (archived={archived}): {len(documents)} documents so far")

    # Topic pass: one query per topic, restricted to the same categories
    for topic_id, topic_name in topic_labels.items():
        for archived in archive_flags:
            for doc in fetch_all(session, token, list(categories), [topic_id], archive=archived):
                record = documents.get(doc["document_id"])
                if record and topic_name not in record["topics"]:
                    record["topics"].append(topic_name)

    logger.info(f"Tagged documents across {len(topic_labels)} topics")

    ingested_at = datetime.now(UTC).isoformat()
    publications = sorted(documents.values(), key=lambda doc: doc["uploaded_date"] or "", reverse=True)
    publications = [
        {**doc, "institution": "BMA", "source": "bma", "ingestion_timestamp": ingested_at} for doc in publications
    ]

    return publications[:max_results] if max_results else publications
