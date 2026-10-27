"""Rerank search hits by how directly they answer the question, using the chat model.

Search ranks by similarity to the query, which is fast but loose: a chunk that mentions "BSCR ratio"
in passing can outrank the table that states it. One chat call reads the question with every candidate
and puts the ones that answer it first, dropping those that don't help.
"""

from fcr_curator.gateway import Chat, chat_json
from fcr_curator.search import SearchHit

DEFAULT_TOP_K = 5
# Characters of each candidate shown to the reranker: enough to judge it, while keeping the prompt small
EXCERPT_CHARS = 800

# FCRs often give the figures an answer is worked out from (capital and requirement amounts) rather than
# the answer itself (a ratio), so excerpts that help even partly must count, or the reranker drops them all
INSTRUCTIONS = """Rank FCR excerpts by how useful they are for answering the question.
Reply with JSON only: {"ranking": [excerpt numbers, most useful first]}
An excerpt is useful if it answers the question or holds facts or figures needed to work the answer out.
Leave out only excerpts that are unrelated to the question."""


def _candidates(hits: list[SearchHit]) -> str:
    return "\n\n".join(
        f"[{number}] {hit.company} FCR {hit.reporting_year}\n{hit.text[:EXCERPT_CHARS]}"
        for number, hit in enumerate(hits, start=1)
    )


def rerank(question: str, hits: list[SearchHit], chat: Chat, top_k: int = DEFAULT_TOP_K) -> list[SearchHit]:
    """Return up to top_k hits in the chat model's order of relevance; [] if it finds none relevant.

    Excerpt numbers the model invents or repeats are ignored.
    """
    if not hits:
        return []
    reply = chat_json(chat, INSTRUCTIONS, f"Question: {question}\n\nExcerpts:\n{_candidates(hits)}")
    numbers = [number for number in reply.get("ranking", []) if isinstance(number, int) and 1 <= number <= len(hits)]
    return [hits[number - 1] for number in dict.fromkeys(numbers)][:top_k]
