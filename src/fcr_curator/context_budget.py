"""Pack the best search hits into a prompt context that fits a token budget, each with its citation.

The context is what the chat model will answer from, so it should hold the most relevant chunks, and
only as many as the budget allows: more text costs more and buries the answer.
"""

from dataclasses import dataclass
from math import ceil

from fcr_curator.search import SearchHit

# Tokens are estimated rather than counted (the model's tokenizer isn't available here). Prose runs about
# 4 characters a token and HTML tables nearer 2, so 3 errs on the side of staying under budget.
CHARS_PER_TOKEN = 3
DEFAULT_MAX_TOKENS = 2000


@dataclass(frozen=True)
class Context:
    """The text to put in the prompt, the hits it holds (numbered as cited), and its estimated size."""

    text: str
    sources: tuple[SearchHit, ...]
    tokens: int


def estimate_tokens(text: str) -> int:
    """Return a conservative estimate of text's length in tokens."""
    return ceil(len(text) / CHARS_PER_TOKEN)


def cite(number: int, hit: SearchHit) -> str:
    """Return a hit as a numbered, citable source: institution, year, page and URL."""
    page = f", page {hit.page}" if hit.page is not None else ""
    return f"[{number}] {hit.institution}, FCR {hit.reporting_year}{page}\n{hit.text}\nSource: {hit.file_url}"


def build_context(hits: list[SearchHit], max_tokens: int = DEFAULT_MAX_TOKENS) -> Context:
    """Return the hits, best first, that fit within max_tokens; a hit too big for the space left is skipped."""
    sources: list[SearchHit] = []
    blocks: list[str] = []
    tokens = 0
    for hit in hits:
        block = cite(len(sources) + 1, hit)
        block_tokens = estimate_tokens(block)
        if tokens + block_tokens > max_tokens:
            continue
        sources.append(hit)
        blocks.append(block)
        tokens += block_tokens
    return Context(text="\n\n".join(blocks), sources=tuple(sources), tokens=tokens)
