"""Call Unity Gateway model services: embeddings and chat.

SQL ai_query reaches only the Databricks-provided system.ai model services, not ones a workspace creates
such as course_ops.gateway.embed, so calls go through the gateway's OpenAI-compatible API instead. They stay
governed by the model service: its permissions, rate limits and usage tracking apply.

Callers take an EmbedBatch or Chat function rather than a client, so tests can pass a fake.
"""

import json
import re
from collections.abc import Callable, Sequence

from databricks.sdk import WorkspaceClient
from openai import OpenAI

GATEWAY_API_PATH = "/ai-gateway/mlflow/v1"
# Inputs per request; well under the gateway's request limits for chunk-sized texts
EMBED_BATCH_SIZE = 64

# Long enough for a reasoning model to think before a short JSON reply
CHAT_MAX_TOKENS = 2000
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

EmbedBatch = Callable[[list[str]], list[list[float]]]
Chat = Callable[[str, str], str]  # (instructions, message) -> reply text


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


def gateway_chat(client: OpenAI, model_service: str) -> Chat:
    """Return a function that sends instructions and a message to a chat model service and returns its reply."""

    def chat(instructions: str, message: str) -> str:
        response = client.chat.completions.create(
            model=model_service,
            max_tokens=CHAT_MAX_TOKENS,
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": message}],
        )
        return response.choices[0].message.content or ""

    return chat


def chat_json(chat: Chat, instructions: str, message: str) -> dict:
    """Ask for a JSON object and return it parsed, ignoring any text or code fence around it.

    Raises ValueError, quoting the start of the reply, when there's no valid JSON object in it.
    """
    reply = chat(instructions, message)
    match = JSON_OBJECT.search(reply)
    try:
        return json.loads(match.group()) if match else json.loads(reply)
    except json.JSONDecodeError as error:
        raise ValueError(f"Expected a JSON object from the chat model, got: {reply[:200]!r}") from error
