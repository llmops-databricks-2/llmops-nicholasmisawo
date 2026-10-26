"""Tests for the FCR curation rules."""

from datetime import date

import pytest

from fcr_curator.curation import (
    DOC_KINDS,
    classify_doc_kind,
    curate,
    file_type,
    institution,
    institution_key,
    keep_doc_kinds,
    keep_reported_since,
    normalize_title,
    on_domain,
    reporting_year,
    same_institution,
)

TODAY = date(2026, 10, 25)


def raw(document_id: str, title: str, url: str, **extra: object) -> dict:
    """Build a bronze row as the ingestion notebook writes it."""
    return {
        "document_id": document_id,
        "title": title,
        "pdf_url": url,
        "domain": "example-re.com",
        "source": "serpapi",
        "search_rank": 1,
        "is_listed": True,
        "first_seen_at": "2026-10-25T00:00:00+00:00",
        **extra,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/files/FCR-2024.pdf", "pdf"),
        ("https://x.com/files/FCR-2024.PDF?v=2", "pdf"),
        ("https://x.com/download?id=7", ""),
    ],
)
def test_file_type(url: str, expected: str) -> None:
    """The file type is the lower-cased extension of the URL path, ignoring any query string."""
    assert file_type(url) == expected


def test_normalize_title_drops_google_ellipsis_and_fixes_dashes() -> None:
    """A title Google truncated loses its ellipsis, and en dashes become hyphens."""
    assert normalize_title("Example Re – Financial Condition Report ...") == "Example Re - Financial Condition Report"


@pytest.mark.parametrize(
    ("title", "url", "snippet", "expected"),
    [
        ("Example Re Ltd. Financial Condition Report 2024", "https://x.com/a.pdf", None, "fcr"),
        ("Document", "https://x.com/FCR_2024_final.pdf", None, "fcr"),
        ("Example Re Ltd. Solvency and Financial Condition Report 2024", "https://example-re.com/a.pdf", None, "fcr"),
        ("Document", "https://example-re.com/SFCR_2024.pdf", None, "fcr"),
        ("Example Re", "https://x.com/report.pdf", "The Financial Condition Report sets out", "fcr"),
        ("Annual Report 2024", "https://x.com/annual.pdf", "Results for the year", "other"),
    ],
)
def test_classify_doc_kind(title: str, url: str, snippet: str | None, expected: str) -> None:
    """Either name of the report, or its abbreviation, in the title, filename or snippet makes it an FCR, on any domain."""
    assert classify_doc_kind(title, url, snippet) == expected


def test_reporting_year_prefers_a_year_beside_the_report_name_over_an_upload_date() -> None:
    """A filename upload date doesn't win over the year the report names."""
    assert reporting_year("Document", "https://x.com/2025-04-30-FCR-2023.pdf", today=TODAY) == 2023


def test_reporting_year_falls_back_to_the_snippet() -> None:
    """When neither title nor filename has a year, the snippet's year-end is used."""
    snippet = "Financial Condition Report for the year ended December 31, 2024"

    assert reporting_year("Financial Condition Report", "https://x.com/report.pdf", snippet, today=TODAY) == 2024


@pytest.mark.parametrize("title", ["FCR 2026", "FCR 2012", "Financial Condition Report"])
def test_reporting_year_is_none_without_a_plausible_year(title: str) -> None:
    """The current year can't be reported on yet, years before the disclosure rules are noise."""
    assert reporting_year(title, "https://x.com/report.pdf", today=TODAY) is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Example Re Ltd. - Financial Condition Report 2024", "Example Re Ltd."),
        ("Example Re, Ltd. | FCR | For the year ended 31 December 2022", "Example Re, Ltd."),
        ("2024 Financial Condition Report", "Site Name"),
    ],
)
def test_institution_is_the_title_before_the_report_name_else_the_site(title: str, expected: str) -> None:
    """The institution comes from the title; a title that starts with the report's name falls back to the site."""
    assert institution(title, "Site Name") == expected


def test_curate_keeps_pdfs_only() -> None:
    """Results that aren't PDFs can't be parsed, so they're dropped."""
    documents = curate(
        [
            raw("a", "Example Re Ltd. FCR 2024", "https://x.com/a.pdf"),
            raw("b", "Example Re Ltd. FCR 2024", "https://x.com/download?id=7"),
        ],
        today=TODAY,
    )

    assert [document["document_id"] for document in documents] == ["a"]


def test_curate_treats_an_extensionless_url_from_a_filetype_pdf_query_as_a_pdf() -> None:
    """Google's filetype:pdf returns only PDFs, so a download link without .pdf is still kept."""
    [document] = curate(
        [raw("a", "Example Re Ltd. FCR 2024", "https://x.com/download?id=7", search_queries=["fcr filetype:pdf"])],
        today=TODAY,
    )

    assert document["file_type"] == "pdf"


@pytest.mark.parametrize("domain", ["bma.bm", "cdn.bma.bm", "osc.ny.gov"])
def test_curate_drops_results_from_excluded_domains_and_their_subdomains(domain: str) -> None:
    """Nothing on bma.bm or a .gov site is curated, however FCR-like it looks."""
    documents = curate([raw("a", "Example Re Ltd. FCR 2024", f"https://{domain}/a.pdf", domain=domain)], today=TODAY)

    assert documents == []


def test_curate_keeps_domains_that_only_end_in_the_same_letters() -> None:
    """Only bma.bm and its subdomains are excluded, not a domain like notbma.bm."""
    documents = curate(
        [raw("a", "Example Re Ltd. FCR 2024", "https://notbma.bm/a.pdf", domain="notbma.bm")], today=TODAY
    )

    assert [document["document_id"] for document in documents] == ["a"]


def test_curate_keeps_one_fcr_per_institution_and_year_preferring_the_best_rank() -> None:
    """The same FCR at two URLs on one domain is one document, the better-ranked one recording the other."""
    documents = curate(
        [
            raw("copy", "Example Re Ltd. FCR 2024", "https://x.com/cdn/a.pdf", search_rank=7),
            raw("original", "Example Re Ltd. - Financial Condition Report 2024", "https://x.com/a.pdf"),
        ],
        today=TODAY,
    )

    assert [(document["document_id"], document["superseded_ids"]) for document in documents] == [("original", ["copy"])]


def test_curate_keeps_each_year_and_each_institution_on_a_shared_domain() -> None:
    """A group site hosting several subsidiaries' FCRs keeps one per subsidiary and year."""
    documents = curate(
        [
            raw("re_2024", "Example Re Ltd. FCR 2024", "https://x.com/re-2024.pdf"),
            raw("re_2023", "Example Re Ltd. FCR 2023", "https://x.com/re-2023.pdf"),
            raw("ins_2024", "Example Insurance Ltd. FCR 2024", "https://x.com/ins-2024.pdf"),
        ],
        today=TODAY,
    )

    assert {document["document_id"] for document in documents} == {"re_2024", "re_2023", "ins_2024"}


def test_curate_prefers_a_result_search_still_returns() -> None:
    """A copy no longer returned by search loses to one that still is, whatever its rank."""
    documents = curate(
        [
            raw("gone", "Example Re Ltd. FCR 2024", "https://x.com/old.pdf", is_listed=False),
            raw("current", "Example Re Ltd. FCR 2024", "https://x.com/new.pdf", search_rank=9),
        ],
        today=TODAY,
    )

    assert [document["document_id"] for document in documents] == ["current"]


def test_keep_doc_kinds_filters_and_rejects_unknown_kinds() -> None:
    """Only listed kinds are kept, and a misspelt kind fails rather than emptying silver."""
    documents = [{"doc_kind": kind} for kind in DOC_KINDS]

    assert keep_doc_kinds(documents, {"fcr"}) == [{"doc_kind": "fcr"}]
    with pytest.raises(ValueError, match="non-empty subset"):
        keep_doc_kinds(documents, {"fcrs"})
    with pytest.raises(ValueError, match="non-empty subset"):
        keep_doc_kinds(documents, set())


def test_keep_reported_since_drops_older_and_undated_documents() -> None:
    """With a cutoff, only documents reporting on that year or later are kept; None keeps all."""
    documents = [{"reporting_year": 2019}, {"reporting_year": 2020}, {"reporting_year": None}]

    assert keep_reported_since(documents, 2020) == [{"reporting_year": 2020}]
    assert keep_reported_since(documents, None) == documents


def test_curate_leaves_the_bronze_rows_it_was_given_unchanged() -> None:
    """Curation returns new rows; the caller's rows aren't modified."""
    rows = [
        raw("copy", "Example Re Ltd. FCR 2024", "https://x.com/cdn/a.pdf", search_rank=7),
        raw("original", "Example Re Ltd. FCR 2024", "https://x.com/a.pdf"),
    ]
    before = [dict(row) for row in rows]

    curate(rows, today=TODAY)

    assert rows == before


def test_institution_key_ignores_case_punctuation_and_legal_suffixes() -> None:
    """'Wilton Re Ltd.' and 'wilton re' are the same insurer."""
    assert institution_key("Wilton Re Ltd.") == institution_key("wilton re") == "wiltonre"


@pytest.mark.parametrize(
    ("name", "other"),
    [("Everest Re", "Everest Reinsurance (Bermuda) Ltd"), ("Hamilton Insurance Group", "Hamilton Insurance Group")],
)
def test_same_institution_matches_a_name_and_its_longer_form(name: str, other: str) -> None:
    """A short name matches the full name it starts, in either order."""
    assert same_institution(name, other) and same_institution(other, name)


@pytest.mark.parametrize(
    ("name", "other"),
    [("Arch Capital Group", "Group"), ("Argo Group", "Cargo Insurance"), ("Re", "Renaissance Reinsurance")],
)
def test_same_institution_rejects_generic_short_and_embedded_names(name: str, other: str) -> None:
    """A bare suffix, a key too short to be specific, or a name inside another word is not a match."""
    assert not same_institution(name, other)


def test_curate_leaves_registered_name_unset_without_a_reference_list() -> None:
    """Without a resolver there's nothing to match against, so no FCR gets a registered name."""
    [document] = curate([raw("a", "Example Re Ltd. FCR 2024", "https://x.com/a.pdf")], today=TODAY)

    assert document["registered_name"] is None


def test_curate_names_each_fcr_with_the_resolvers_answer() -> None:
    """The resolver sees the curated fields and its answer becomes registered_name."""
    seen: list[dict] = []

    def resolve(document: dict) -> str:
        seen.append(document)
        return "Example Re"

    [document] = curate(
        [raw("a", "Example Re Ltd. FCR 2024", "https://x.com/a.pdf")], today=TODAY, resolve_entity=resolve
    )

    assert document["registered_name"] == "Example Re"
    assert seen[0]["institution"] == "Example Re Ltd." and seen[0]["domain"] == "example-re.com"


@pytest.mark.parametrize(
    ("host", "expected"), [("cdn.bma.bm", True), ("bma.bm", True), ("notbma.bm", False), (None, False)]
)
def test_on_domain_matches_the_domain_and_its_subdomains_only(host: str | None, expected: bool) -> None:
    """A subdomain is on its parent domain; a different domain ending in the same letters isn't."""
    assert on_domain(host, "bma.bm") is expected


def test_curate_keeps_an_fcr_and_annual_accounts_for_the_same_insurer_and_year() -> None:
    """Different kinds of document aren't duplicates, so the accounts can't push out the FCR."""
    documents = curate(
        [
            raw("accounts", "Example Re Annual Financial Statements 2024", "https://x.com/accounts-2024.pdf"),
            raw("fcr", "Example Re Financial Condition Report 2024", "https://x.com/fcr-2024.pdf"),
        ],
        today=TODAY,
    )

    assert {document["doc_kind"] for document in documents} == {"fcr", "other"}


def test_curate_keeps_one_row_for_a_file_found_by_two_sources() -> None:
    """The same PDF from a search and from a disclosures page is one document, not two."""
    url = "https://x.com/example-re-fcr-2024.pdf"
    documents = curate(
        [
            raw("serpapi_a", "Example Re Ltd. FCR 2024", url, search_rank=2),
            raw("pages_a", "View", url, search_rank=None, site_name="Example Re"),
        ],
        today=TODAY,
    )

    assert [(document["document_id"], document["superseded_ids"]) for document in documents] == [
        ("serpapi_a", ["pages_a"])
    ]
