"""Tests for embedding chunk text through a Unity Gateway model service."""

from types import SimpleNamespace

import pytest

from fcr_curator.embeddings import embed_texts, gateway_embedder


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
