"""Two ways to split a parsed FCR into chunks, so they can be compared side by side.

Both read the elements ai_parse_document found (text, section_header, table, page_header, ...).

1. element_chunks: one chunk per text or table element. Simple, but sizes vary wildly (a lone
   company name is a chunk; a whole table can be 36k characters) and section headings are lost.
2. section_chunks: chunks follow the document's sections, like Unstructured's by_title strategy.
   Every chunk starts with its section heading, short sections are merged, long text and tables
   are split to a size limit.

Plain Python, so the rules can be tested without Spark.
"""

import json
import re
from typing import NamedTuple

# Strategy 1 keeps these element types; titles, headers, footers and page numbers are dropped
ELEMENT_CHUNK_TYPES = ("text", "table")

# Strategy 2
HEADING_TYPES = ("title", "section_header")  # Start a new section
TEXT_TYPES = ("text", "caption")  # Packed together within a section
MAX_CHUNK_CHARS = 2000  # About 500 tokens
MIN_SECTION_CHARS = 200  # A shorter section is merged into the next one
MAX_HEADING_CHARS = 200  # Keeps a runaway "heading" from crowding out the text


class Chunk(NamedTuple):
    """One piece of an FCR."""

    chunk_id: str  # Unique within the FCR
    page_id: int | None  # Page it starts on, for citations
    text: str


def clean_chunk(text: str) -> str:
    """Rejoin words split by a hyphen at a line break, and collapse all whitespace to single spaces."""
    joined = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text or "")  # "docu-\nments" -> "documents"
    return re.sub(r"\s+", " ", joined).strip()


def _elements(parsed_content_json: str | None) -> list[dict]:
    if not parsed_content_json:
        return []
    return json.loads(parsed_content_json).get("document", {}).get("elements", [])


def _first_page(element: dict) -> int | None:
    boxes = element.get("bbox") or []
    return boxes[0].get("page_id") if boxes else None


# --- Strategy 1: one chunk per element --------------------------------------------------------


def element_chunks(parsed_content_json: str | None) -> list[Chunk]:
    """Return one chunk per non-empty text or table element, keyed by the element's id."""
    chunks = []
    for element in _elements(parsed_content_json):
        text = clean_chunk(element.get("content", ""))
        if element.get("type") in ELEMENT_CHUNK_TYPES and text:
            chunks.append(Chunk(str(element.get("id", "")), _first_page(element), text))
    return chunks


# --- Strategy 2: one chunk per section, size-limited ------------------------------------------


def split_text(text: str, limit: int) -> list[str]:
    """Split text into pieces of at most limit characters, at sentence ends where possible."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        for part in (sentence[i : i + limit] for i in range(0, len(sentence), limit)):  # Cut an over-long sentence
            if current and len(current) + 1 + len(part) > limit:
                pieces.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
    return [*pieces, current] if current else pieces


def split_table(html: str, limit: int) -> list[str]:
    """Split an HTML table into tables of at most about limit characters, each repeating the header row."""
    if len(html) <= limit:
        return [html]
    rows = re.findall(r"<tr\b.*?</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    if len(rows) < 2:
        return split_text(html, limit)  # Not a table we can split by rows

    header, groups, current = rows[0], [], []
    for row in rows[1:]:
        if current and len(header) + sum(map(len, current)) + len(row) > limit:
            groups.append(current)
            current = []
        current.append(row)
    groups.append(current)
    return [f"<table>{header}{''.join(group)}</table>" for group in groups]


class _SectionChunker:
    """Builds section chunks one element at a time; see section_chunks."""

    def __init__(self, max_chars: int, min_chars: int) -> None:
        self.max_chars, self.min_chars = max_chars, min_chars
        self.chunks: list[Chunk] = []
        self.heading = ""  # Heading of the section being read
        self.chunk_heading = ""  # Heading shown at the top of the chunk being built
        self.body = ""  # Text of the chunk being built
        self.page: int | None = None  # Page the chunk being built starts on

    def _emit(self, heading: str, body: str, page: int | None) -> None:
        text = f"{heading}\n{body}" if heading else body
        self.chunks.append(Chunk(str(len(self.chunks)), page, text))

    def _room(self) -> int:
        # Space left for text once the heading is added; never less than half a chunk
        return max(self.max_chars - len(self.heading) - 1, self.max_chars // 2)

    def flush(self) -> None:
        """Finish the chunk being built, if any."""
        if self.body:
            self._emit(self.chunk_heading, self.body, self.page)
        self.body = ""

    def add_heading(self, heading: str) -> None:
        """Start a new section, unless the current chunk is too short to stand alone."""
        if len(self.body) >= self.min_chars:
            self.flush()
        if self.body:  # Short section: carry on, keeping the new heading inline
            self.body += f"\n{heading}"
        self.heading = heading

    def add_text(self, text: str, page: int | None) -> None:
        """Pack text into the current chunk, starting a new one when it's full."""
        for piece in split_text(text, self._room()):
            if self.body and len(self.body) + 1 + len(piece) > self._room():
                self.flush()
            if self.body:
                self.body += f" {piece}"
            else:
                self.chunk_heading, self.body, self.page = self.heading, piece, page

    def add_table(self, html: str, page: int | None) -> None:
        """Give a table chunks of its own, split by rows if it's too big."""
        self.flush()
        for piece in split_table(html, self._room()):
            self._emit(self.heading, piece, page)


def section_chunks(
    parsed_content_json: str | None, max_chars: int = MAX_CHUNK_CHARS, min_chars: int = MIN_SECTION_CHARS
) -> list[Chunk]:
    """Return chunks that follow the FCR's sections, each starting with its section heading.

    Headings start sections, text and captions are packed up to max_chars, sections shorter than
    min_chars are merged into the next, and tables get chunks of their own. Page headers, footers,
    page numbers, figures and signatures are dropped.
    """
    chunker = _SectionChunker(max_chars, min_chars)
    for element in _elements(parsed_content_json):
        text = clean_chunk(element.get("content", ""))
        if not text:
            continue
        kind = element.get("type")
        if kind in HEADING_TYPES:
            chunker.add_heading(text[:MAX_HEADING_CHARS])
        elif kind in TEXT_TYPES:
            chunker.add_text(text, _first_page(element))
        elif kind == "table":
            chunker.add_table(text, _first_page(element))
    chunker.flush()
    return chunker.chunks
