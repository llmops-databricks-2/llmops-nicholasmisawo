"""Tests for turning ai_parse_document output into retrievable chunks."""

import json

from fcr_curator.data_processor import Chunk, clean_chunk, extract_chunks


def parsed(*elements: dict) -> str:
    """Build an ai_parse_document v2 result holding the given elements, as JSON."""
    return json.dumps({"document": {"pages": [], "elements": list(elements)}, "error_status": []})


def element(element_id: int, element_type: str, content: str, page_id: int | None = 0) -> dict:
    """Build one parsed element, optionally placed on a page."""
    bbox = [] if page_id is None else [{"coord": [0, 0, 1, 1], "page_id": page_id}]
    return {"id": element_id, "type": element_type, "content": content, "bbox": bbox}


def test_clean_chunk_rejoins_words_hyphenated_across_lines() -> None:
    """A word split by a line-break hyphen is rejoined."""
    assert clean_chunk("insur-\n  ance licence") == "insurance licence"


def test_clean_chunk_collapses_newlines_and_whitespace() -> None:
    """Newlines and runs of whitespace become single spaces, and the ends are trimmed."""
    assert clean_chunk("  Class 3A\n\ninsurers   must\tfile  ") == "Class 3A insurers must file"


def test_extract_chunks_keeps_text_and_tables_only() -> None:
    """Titles, headers, footers and page numbers are left out; text and tables are kept in order."""
    content = parsed(
        element(0, "title", "Financial Condition Report"),
        element(1, "text", "The Company's BSCR ratio"),
        element(2, "page_footer", "Example Re"),
        element(3, "table", "<table><tr><td>Capital</td></tr></table>"),
        element(4, "page_number", "2"),
    )

    assert [chunk.chunk_id for chunk in extract_chunks(content)] == ["1", "3"]


def test_extract_chunks_records_cleaned_text_and_first_page() -> None:
    """Each chunk carries its element id as a string, its first page, and cleaned text."""
    content = parsed(element(7, "text", "solvency ratio\nof 250%", page_id=4))

    assert extract_chunks(content) == [Chunk("7", 4, "solvency ratio of 250%")]


def test_extract_chunks_leaves_page_unset_without_a_bounding_box() -> None:
    """An element with no bounding box has no page."""
    content = parsed(element(1, "text", "Annex", page_id=None))

    assert extract_chunks(content)[0].page_id is None


def test_extract_chunks_drops_elements_empty_after_cleaning() -> None:
    """Whitespace-only text would embed as noise, so it isn't a chunk."""
    assert extract_chunks(parsed(element(1, "text", " \n\t "))) == []


def test_extract_chunks_returns_nothing_for_missing_content() -> None:
    """A document whose parse produced no content yields no chunks rather than failing."""
    assert extract_chunks(None) == []
    assert extract_chunks(json.dumps({"error_status": [{"error_message": "x"}]})) == []
