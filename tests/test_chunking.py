"""Tests for the two chunking strategies that turn ai_parse_document output into chunks."""

import json

from fcr_curator.chunking import Chunk, clean_chunk, element_chunks, section_chunks, split_table, split_text


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


def test_element_chunks_keeps_text_and_tables_only() -> None:
    """Titles, headers, footers and page numbers are left out; text and tables are kept in order."""
    content = parsed(
        element(0, "title", "Financial Condition Report"),
        element(1, "text", "The Company's BSCR ratio"),
        element(2, "page_footer", "Example Re"),
        element(3, "table", "<table><tr><td>Capital</td></tr></table>"),
        element(4, "page_number", "2"),
    )

    assert [chunk.chunk_id for chunk in element_chunks(content)] == ["1", "3"]


def test_element_chunks_records_cleaned_text_and_first_page() -> None:
    """Each chunk carries its element id as a string, its first page, and cleaned text."""
    content = parsed(element(7, "text", "solvency ratio\nof 250%", page_id=4))

    assert element_chunks(content) == [Chunk("7", 4, "solvency ratio of 250%")]


def test_element_chunks_leaves_page_unset_without_a_bounding_box() -> None:
    """An element with no bounding box has no page."""
    content = parsed(element(1, "text", "Annex", page_id=None))

    assert element_chunks(content)[0].page_id is None


def test_element_chunks_drops_elements_empty_after_cleaning() -> None:
    """Whitespace-only text would embed as noise, so it isn't a chunk."""
    assert element_chunks(parsed(element(1, "text", " \n\t "))) == []


def test_element_chunks_returns_nothing_for_missing_content() -> None:
    """A document whose parse produced no content yields no chunks rather than failing."""
    assert element_chunks(None) == []
    assert element_chunks(json.dumps({"error_status": [{"error_message": "x"}]})) == []


# --- Strategy 2: section chunks ---------------------------------------------------------------


def texts(content: str, **limits: int) -> list[str]:
    """Return the text of each section chunk, with small limits by default so tests stay short."""
    return [chunk.text for chunk in section_chunks(content, **{"max_chars": 60, "min_chars": 20, **limits})]


def test_section_chunks_start_with_their_section_heading() -> None:
    """Text under a heading is packed into one chunk that begins with the heading."""
    content = parsed(
        element(0, "section_header", "Capital Management"),
        element(1, "text", "BSCR ratio of 250%."),
        element(2, "text", "Eligible capital rose."),
    )

    assert texts(content, max_chars=80) == ["Capital Management\nBSCR ratio of 250%. Eligible capital rose."]


def test_section_chunks_start_a_new_chunk_at_each_long_enough_section() -> None:
    """A section that has reached min_chars ends its chunk when the next heading arrives."""
    content = parsed(
        element(0, "section_header", "Risk Profile"),
        element(1, "text", "Catastrophe risk dominates."),
        element(2, "section_header", "Governance"),
        element(3, "text", "The board meets quarterly."),
    )

    assert texts(content) == ["Risk Profile\nCatastrophe risk dominates.", "Governance\nThe board meets quarterly."]


def test_section_chunks_merge_a_short_section_into_the_next() -> None:
    """A section under min_chars carries on into the next, keeping the new heading inline."""
    content = parsed(
        element(0, "section_header", "Auditor"),
        element(1, "text", "KPMG."),
        element(2, "section_header", "Actuary"),
        element(3, "text", "Example LLP."),
    )

    assert texts(content) == ["Auditor\nKPMG.\nActuary Example LLP."]


def test_section_chunks_split_long_text_and_repeat_the_heading() -> None:
    """Text longer than max_chars is split at sentence ends, and every piece keeps the heading."""
    content = parsed(element(0, "section_header", "Solvency"), element(1, "text", "First sentence here. " * 6))

    chunks = texts(content)
    assert len(chunks) > 1
    assert all(chunk.startswith("Solvency\n") and len(chunk) <= 60 for chunk in chunks)


def test_section_chunks_give_tables_their_own_chunk() -> None:
    """A table ends the text chunk before it and is never mixed with text."""
    table = "<table><tr><td>BSCR</td></tr></table>"
    content = parsed(
        element(0, "section_header", "Capital"),
        element(1, "text", "Figures follow."),
        element(2, "table", table),
        element(3, "text", "After the table."),
    )

    assert texts(content, min_chars=0) == ["Capital\nFigures follow.", f"Capital\n{table}", "Capital\nAfter the table."]


def test_section_chunks_drop_page_furniture() -> None:
    """Page headers, footers, page numbers, figures and signatures never reach a chunk."""
    content = parsed(
        element(0, "page_header", "Example Re Ltd."),
        element(1, "text", "Body text."),
        element(2, "page_footer", "Confidential"),
        element(3, "page_number", "7"),
        element(4, "figure", "chart"),
        element(5, "signature", "J. Smith"),
    )

    assert texts(content) == ["Body text."]


def test_section_chunks_number_chunks_and_keep_the_first_page() -> None:
    """Chunk ids count up from 0, and each chunk records the page its text starts on."""
    content = parsed(element(0, "text", "On page three.", page_id=3), element(1, "table", "<table></table>", page_id=4))

    assert [(chunk.chunk_id, chunk.page_id) for chunk in section_chunks(content)] == [("0", 3), ("1", 4)]


def test_split_table_repeats_the_header_row_in_every_piece() -> None:
    """An oversized table is split by rows, and each piece is a table starting with the header row."""
    header = "<tr><th>Item</th></tr>"
    rows = "".join(f"<tr><td>row {i}</td></tr>" for i in range(6))

    pieces = split_table(f"<table>{header}{rows}</table>", limit=80)

    assert len(pieces) > 1
    assert all(piece.startswith(f"<table>{header}") and piece.endswith("</table>") for piece in pieces)
    assert sum(piece.count("<td>row") for piece in pieces) == 6


def test_split_text_cuts_a_sentence_longer_than_the_limit() -> None:
    """A single sentence over the limit is cut, so no piece exceeds it."""
    assert all(len(piece) <= 10 for piece in split_text("x" * 35, limit=10))
