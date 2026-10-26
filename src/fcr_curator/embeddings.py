"""Embed text through a Unity Gateway model service.

SQL ai_query reaches only the Databricks-provided system.ai model services, not ones a workspace creates
such as course_ops.gateway.embed, so embeddings go through the gateway's OpenAI-compatible embeddings API
instead. Calls stay governed by the model service: its permissions, rate limits and usage tracking apply.
"""

from collections.abc import Callable, Sequence

from databricks.sdk import WorkspaceClient
from openai import OpenAI

GATEWAY_API_PATH = "/ai-gateway/mlflow/v1"
# Inputs per request; well under the gateway's request limits for chunk-sized texts
EMBED_BATCH_SIZE = 64

EmbedBatch = Callable[[list[str]], list[list[float]]]


def gateway_client(workspace: WorkspaceClient) -> OpenAI:
    """Return an OpenAI client for the workspace's Unity Gateway, authenticated as the workspace caller."""
    # authenticate() yields the same bearer token every Databricks API call uses, whatever the auth type
    token = workspace.config.authenticate()["Authorization"].removeprefix("Bearer ")
    return OpenAI(api_key=token, base_url=f"{workspace.config.host.rstrip('/')}{GATEWAY_API_PATH}")


def gateway_embedder(client: OpenAI, model_service: str) -> EmbedBatch:
    """Return a function that embeds one batch of texts with the given three-part model service name."""

    def embed(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(model=model_service, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    return embed


def embed_texts(texts: Sequence[str], embed_batch: EmbedBatch, batch_size: int = EMBED_BATCH_SIZE) -> list[list[float]]:
    """Embed texts in batches, returning one vector per text in the same order.

    Raises RuntimeError when a batch comes back with a different number of vectors than texts, since
    pairing the rest by position would attach vectors to the wrong chunks.
    """
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        batch_vectors = embed_batch(batch)
        if len(batch_vectors) != len(batch):
            raise RuntimeError(
                f"Embedding batch at {start} returned {len(batch_vectors)} vectors for {len(batch)} texts"
            )
        vectors.extend(batch_vectors)
    return vectors
