"""Tests for the consultation paper curation rules."""

from datetime import date, datetime

import pytest

from consultation_paper_curator.curation import (
    DOC_KIND_PREFIXES,
    DOC_KINDS,
    classify_doc_kind,
    curate,
    extract_emails,
    file_type,
    keep_doc_kinds,
    keep_published_since,
    normalize_title,
    original_upload,
)

CDN = "https://cdn.bma.bm/documents/"


def raw(document_id: str, title: str, url: str, uploaded_date: str = "2026-01-01", **extra: object) -> dict:
    """Build a raw BMA row as the ingestion notebook writes it."""
    return {
        "document_id": document_id,
        "title": title,
        "pdf_url": CDN + url,
        "uploaded_date": uploaded_date,
        "source": "bma",
        "is_archived": False,
        **extra,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CDN + "2026-01-01-00-00-00-Paper.pdf", "pdf"),
        (CDN + "2023-08-22-12-37-34-Group-2024-BSCR.xlsm", "xlsm"),
        (CDN + "2023-05-22-15-08-57-Recovery-Rules.docx.pdf", "pdf"),
    ],
)
def test_file_type(url: str, expected: str) -> None:
    """The file type is the lower-cased final extension of the URL."""
    assert file_type(url) == expected


def test_normalize_title_fixes_quotes_and_dashes() -> None:
    """Curly quotes, en dashes and CSV-doubled quotes become plain ASCII."""
    assert (
        normalize_title('"Stakeholder Letter – Updates to ""Proposed Enhancements"""')
        == 'Stakeholder Letter - Updates to "Proposed Enhancements"'
    )


def test_normalize_title_collapses_repeated_prefix() -> None:
    """A title prefix repeated by mistake appears only once."""
    assert normalize_title("Discussion Paper - Discussion Paper - Climate Risks") == "Discussion Paper - Climate Risks"


@pytest.mark.parametrize(
    ("title", "url", "expected"),
    [
        ("Consultation Paper - Basic Bank Accounts", "x.pdf", "consultation_paper"),
        ("NAMLC Consultation Paper - Trustee Act", "x.pdf", "consultation_paper"),
        ("Discussion Paper - Asset Tokenisation", "x.pdf", "discussion_paper"),
        ("Stakeholder Letter - Feedback", "x.pdf", "stakeholder_letter"),
        ("Notice - SCPS Consultation Guidance", "x.pdf", "notice"),
        ("Guidance Notes for Applications", "x.pdf", "guidance"),
        ("Digital Asset Issuance Act 2020", "x.pdf", "legislation"),
        # The title hides the kind; the filename gives it away
        (
            "Proposed Amendments for the Regulation of Investment Exchanges",
            "2023-11-16-13-49-40-Stakeholder-Letter---Proposed-Amendments.pdf",
            "stakeholder_letter",
        ),
    ],
)
def test_classify_doc_kind(title: str, url: str, expected: str) -> None:
    """Documents are classified by title prefix, then filename, then keywords."""
    assert classify_doc_kind(title, CDN + url) == expected


def test_original_upload_uses_last_timestamp_of_reuploaded_file() -> None:
    """A re-uploaded file keeps its original upload time; untimestamped files have none."""
    url = CDN + "2026-01-09-10-55-51-2023-09-22-16-25-47-Amendment-Rules-2024.pdf"
    assert original_upload(url) == datetime(2023, 9, 22, 16, 25, 47)
    assert original_upload(CDN + "Paper.pdf") is None


def test_extract_emails() -> None:
    """Submission addresses are found and lower-cased; missing notes give none."""
    note = "Please email your comments to Policy@bma.bm by 15 December 2026."
    assert extract_emails(note) == ["policy@bma.bm"]
    assert extract_emails(None) == []


def test_curate_prefers_url_date_and_parses_deadline() -> None:
    """The CDN upload date wins over the listed date, which is kept alongside."""
    [doc] = curate(
        [
            raw(
                "bma_11130",
                "Consultation Paper - New Insurer Class",
                "2026-01-21-14-43-53-Consultation-Paper.pdf",
                uploaded_date="2026-02-27",
                comment_deadline="2026-02-27",
            )
        ]
    )
    assert doc["published_date"] == date(2026, 1, 21)
    assert doc["listed_date"] == date(2026, 2, 27)
    assert doc["comment_deadline"] == date(2026, 2, 27)


def test_curate_drops_spreadsheets() -> None:
    """Only PDFs survive curation."""
    docs = curate(
        [
            raw("bma_1", "Consultation Paper - A", "2026-01-01-00-00-00-A.pdf"),
            raw("bma_2", "Class 4 2024 BSCR", "2026-01-01-00-00-00-BSCR.xlsm"),
        ]
    )
    assert [d["document_id"] for d in docs] == ["bma_1"]


def test_curate_keeps_current_over_archived_duplicate() -> None:
    """Of two same-titled documents the current one survives and records the other."""
    docs = curate(
        [
            raw(
                "bma_8150",
                "Consultation Paper - Proposed Enhancements",
                "2023-07-28-16-11-59-CP.pdf",
                is_archived=True,
            ),
            raw(
                "bma_8326",
                "Consultation Paper – Proposed Enhancements",
                "2023-08-24-02-01-31-CP.pdf",
            ),
        ]
    )
    assert [(d["document_id"], d["superseded_ids"]) for d in docs] == [("bma_8326", ["bma_8150"])]


def test_doc_kinds_lists_every_classification() -> None:
    """DOC_KINDS covers each kind the classifier can produce, so download filters can be validated."""
    produced = {kind for _, kind in DOC_KIND_PREFIXES} | {
        "consultation_paper",
        "discussion_paper",
        "legislation",
        "other",
    }

    assert produced == set(DOC_KINDS)


def test_keep_doc_kinds_keeps_only_included_kinds_in_order() -> None:
    """Papers of a listed kind are kept in their original order; every other kind is left out."""
    papers = [
        {"document_id": "bma_1", "doc_kind": "consultation_paper"},
        {"document_id": "bma_2", "doc_kind": "notice"},
        {"document_id": "bma_3", "doc_kind": "legislation"},
        {"document_id": "bma_4", "doc_kind": "consultation_paper"},
    ]

    kept = keep_doc_kinds(papers, {"consultation_paper", "legislation"})

    assert [p["document_id"] for p in kept] == ["bma_1", "bma_3", "bma_4"]


def test_keep_doc_kinds_rejects_empty_set() -> None:
    """Keeping nothing would empty silver, so an empty set fails instead."""
    with pytest.raises(ValueError, match="No doc_kinds"):
        keep_doc_kinds([{"document_id": "bma_1", "doc_kind": "notice"}], set())


def test_keep_doc_kinds_rejects_unknown_doc_kind() -> None:
    """A misspelt kind fails loudly instead of silently dropping that kind's papers."""
    with pytest.raises(ValueError, match="consultation_papers"):
        keep_doc_kinds([], {"consultation_papers"})


def test_keep_published_since_keeps_cutoff_year_and_later_in_order() -> None:
    """Papers from the cutoff year onward are kept in order; older and undated papers are left out."""
    papers = [
        {"document_id": "bma_1", "published_date": date(2024, 3, 1)},
        {"document_id": "bma_2", "published_date": date(2019, 12, 31)},
        {"document_id": "bma_3", "published_date": None},
        {"document_id": "bma_4", "published_date": date(2020, 1, 1)},
    ]

    kept = keep_published_since(papers, 2020)

    assert [p["document_id"] for p in kept] == ["bma_1", "bma_4"]


def test_keep_published_since_none_keeps_everything() -> None:
    """No cutoff keeps every paper, undated ones included."""
    papers = [
        {"document_id": "bma_1", "published_date": date(2001, 1, 1)},
        {"document_id": "bma_2", "published_date": None},
    ]

    assert keep_published_since(papers, None) == papers
