"""Tests for parsing BMA documents-centre HTML cards."""

from datetime import date

from consultation_paper_curator.sources.bma import parse_cards, parse_date


def test_parse_date_finds_first_day_month_year() -> None:
    """The first 'DD Month YYYY' in free text is returned as a date."""
    assert parse_date("closed on 3 July 2026, extended from 1 June 2026") == date(2026, 7, 3)


def test_parse_date_returns_none_without_a_valid_date() -> None:
    """Missing text, no date, or an impossible date all give None."""
    assert [parse_date(None), parse_date("no date here"), parse_date("31 February 2026")] == [None, None, None]


def test_parse_cards_maps_a_card_to_a_raw_row(bma_cards_html: str) -> None:
    """A card becomes a raw row with cleaned title, ISO dates and links."""
    [doc] = parse_cards(bma_cards_html)

    assert doc == {
        "document_id": "bma_12871",
        "title": "Consultation Paper - Resolution Regime",
        "summary": "Please email your comments to policy@bma.bm by 15 December 2026.",
        "comment_deadline": "2026-12-15",
        "uploaded_date": "2026-09-15",
        "document_type": "Regulatory Legislative Documents",
        "is_stakeholder_letter": False,
        "pdf_url": "https://cdn.bma.bm/documents/2026-09-15-17-06-59-CP.pdf",
        "view_url": "https://www.bma.bm/viewPDF/documents/CP.pdf",
        "download_url": "https://www.bma.bm/downloadPDF/documents/CP.pdf",
        "related_documents": ["https://cdn.bma.bm/documents/Notice.pdf"],
    }
