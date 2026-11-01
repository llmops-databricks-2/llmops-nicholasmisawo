"""Tests for calling Unity Gateway model services: embeddings and JSON chat replies."""

from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from fcr_curator.gateway import (
    EMBED_ATTEMPTS,
    RATE_LIMIT_WAIT_SECONDS,
    chat_json,
    embed_texts,
    gateway_embedder,
)


def length_embedder(calls: list[list[str]]) -> object:
    """Return an embedder that records each batch and embeds a text as [its length]."""

    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(text))] for text in texts]

    return embed


def test_embed_texts_splits_into_batches_and_keeps_the_text_order() -> None:
    """Five texts in batches of two make three requests, and each vector lines up with its text."""
    calls: list[list[str]] = []

    vectors = embed_texts(["a", "bb", "ccc", "dddd", "eeeee"], length_embedder(calls), batch_size=2)

    assert [len(batch) for batch in calls] == [2, 2, 1]
    assert vectors == [[1.0], [2.0], [3.0], [4.0], [5.0]]


def test_embed_texts_makes_no_request_for_no_texts() -> None:
    """A run with no new chunks costs no gateway calls."""
    calls: list[list[str]] = []

    assert embed_texts([], length_embedder(calls)) == []
    assert calls == []


def test_embed_texts_fails_when_a_batch_returns_the_wrong_number_of_vectors() -> None:
    """Vectors paired by position would land on the wrong chunks, so a short batch is an error."""
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 texts"):
        embed_texts(["a", "b"], lambda texts: [[0.0]])


def test_gateway_embedder_orders_vectors_by_their_index_and_names_the_model_service() -> None:
    """The response is reordered by index, and the request uses the three-part service name."""
    requests: list[dict] = []
    data = [SimpleNamespace(index=1, embedding=[2.0]), SimpleNamespace(index=0, embedding=[1.0])]

    def create(**request: object) -> SimpleNamespace:
        requests.append(request)
        return SimpleNamespace(data=data)

    client = SimpleNamespace(embeddings=SimpleNamespace(create=create))

    vectors = gateway_embedder(client, "course_ops.gateway.embed")(["first", "second"])

    assert vectors == [[1.0], [2.0]]
    assert requests == [{"model": "course_ops.gateway.embed", "input": ["first", "second"]}]


def rate_limited_client(rejections: int, calls: list[str]) -> SimpleNamespace:
    """Return a client that rejects its first requests with a 429, recording each request."""
    response = httpx.Response(429, request=httpx.Request("POST", "https://gateway.example/embeddings"))

    def create(**request: object) -> SimpleNamespace:
        calls.append("request")
        if len(calls) <= rejections:
            raise RateLimitError("Tokens-per-minute rate limit exceeded", response=response, body=None)
        return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0])])

    return SimpleNamespace(embeddings=SimpleNamespace(create=create))


def test_gateway_embedder_waits_for_the_next_minute_when_rate_limited() -> None:
    """A rejected batch waits a full renewal period each time, then returns its vectors."""
    calls: list[str] = []
    waits: list[float] = []

    embed = gateway_embedder(rate_limited_client(2, calls), "service", sleep=waits.append)

    assert embed(["text"]) == [[1.0]]
    assert waits == [RATE_LIMIT_WAIT_SECONDS, RATE_LIMIT_WAIT_SECONDS]


def test_gateway_embedder_doesnt_wait_when_not_rate_limited() -> None:
    """A batch within the limit goes straight through."""
    waits: list[float] = []

    gateway_embedder(rate_limited_client(0, []), "service", sleep=waits.append)(["text"])

    assert waits == []


def test_gateway_embedder_fails_after_its_last_attempt_is_rate_limited() -> None:
    """A limit that never clears fails the run, without a pointless wait after the last attempt."""
    calls: list[str] = []
    waits: list[float] = []
    embed = gateway_embedder(rate_limited_client(EMBED_ATTEMPTS, calls), "service", sleep=waits.append)

    with pytest.raises(RateLimitError):
        embed(["text"])

    assert len(calls) == EMBED_ATTEMPTS
    assert len(waits) == EMBED_ATTEMPTS - 1


@pytest.mark.parametrize(
    "reply",
    ['{"ranking": [2, 1]}', 'Here you go:\n```json\n{"ranking": [2, 1]}\n```'],
)
def test_chat_json_reads_the_object_even_when_wrapped_in_text(reply: str) -> None:
    """A JSON object is found whether or not the model added prose or a code fence around it."""
    assert chat_json(lambda instructions, message: reply, "i", "m") == {"ranking": [2, 1]}


def test_chat_json_fails_with_the_reply_when_there_is_no_json() -> None:
    """A reply with no JSON object raises an error that quotes it, so the failure can be diagnosed."""
    with pytest.raises(ValueError, match="I cannot help"):
        chat_json(lambda instructions, message: "I cannot help with that", "i", "m")
