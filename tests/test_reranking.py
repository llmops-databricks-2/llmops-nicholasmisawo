"""Tests for reranking search hits with the chat model."""

from fcr_curator.reranking import EXCERPT_CHARS, rerank
from fcr_curator.search import SearchHit


def hits(count: int) -> list[SearchHit]:
    """Build count hits with ids h1, h2, ..."""
    return [SearchHit(f"h{number}", "x" * 2000, "Example Re", 2024, 0, None) for number in range(1, count + 1)]


def replying(reply: str, prompts: list[str] | None = None) -> object:
    """Return a fake chat model that records each message and always gives this reply."""

    def chat(instructions: str, message: str) -> str:
        (prompts if prompts is not None else []).append(message)
        return reply

    return chat


def test_rerank_orders_hits_as_the_model_ranks_them() -> None:
    """The model's excerpt numbers (counted from 1) set the order of the hits returned."""
    reranked = rerank("q", hits(3), replying('{"ranking": [3, 1]}'))

    assert [hit.id for hit in reranked] == ["h3", "h1"]


def test_rerank_keeps_at_most_top_k() -> None:
    """Only the top_k best-ranked hits are returned."""
    reranked = rerank("q", hits(4), replying('{"ranking": [4, 3, 2, 1]}'), top_k=2)

    assert [hit.id for hit in reranked] == ["h4", "h3"]


def test_rerank_ignores_invented_and_repeated_numbers() -> None:
    """Numbers outside the candidates, repeats and non-numbers are skipped."""
    reranked = rerank("q", hits(2), replying('{"ranking": [9, 2, 2, "1", 0]}'))

    assert [hit.id for hit in reranked] == ["h2"]


def test_rerank_shows_the_model_shortened_excerpts() -> None:
    """Each candidate is cut to EXCERPT_CHARS so the prompt stays small."""
    prompts: list[str] = []

    rerank("q", hits(1), replying('{"ranking": [1]}', prompts))

    assert "x" * EXCERPT_CHARS in prompts[0] and "x" * (EXCERPT_CHARS + 1) not in prompts[0]


def test_rerank_of_no_hits_asks_nothing() -> None:
    """With no candidates there's nothing to rank, so the model isn't called."""
    prompts: list[str] = []

    assert rerank("q", [], replying("{}", prompts)) == []
    assert prompts == []
