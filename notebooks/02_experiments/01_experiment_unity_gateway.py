# Databricks notebook source
# Experiment: Market Intelligence Assistant - Unity Gateway Models
#
# Objective:
# - Authenticate using Databricks WorkspaceClient.
# - Test the course chat and embedding model services.
# - Record responses, latency, token usage, and limitations.
# - Evaluate the foundation model before adding RAG and SQL tools.

import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from databricks_openai import DatabricksOpenAI
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAIError,
)

# COMMAND ----------
# 1. Authenticate and configure Unity Gateway


CHAT_MODEL = "course_ops.gateway.chat"
EMBEDDING_MODEL = "course_ops.gateway.embed"

client = DatabricksOpenAI(use_ai_gateway=True)

print("Databricks Unity Gateway client configured successfully.")
print(f"Chat model: {CHAT_MODEL}")
print(f"Embedding model: {EMBEDDING_MODEL}")

# COMMAND ----------
# 2. Define Market Intelligence test prompts

SYSTEM_PROMPT = """
You are Market Intelligence Assistant, specialising in monetary
policy, central bank communications, and foreign exchange markets.

Provide concise, analytical, evidence-based responses.
Distinguish observations from hypotheses.
Do not invent market data, citations, or statistics.
Do not infer causation from correlation.
Acknowledge limitations when live or historical data is unavailable.
"""

test_cases = [
    {
        "test_id": "FX-001",
        "category": "Monetary Policy",
        "prompt": (
            "Explain how differences between ECB and Federal Reserve "
            "monetary policy expectations can influence EUR/USD. "
            "Limit your response to 150 words."
        ),
        "expected_behavior": ("Explain interest-rate differentials, expectations, capital flows, and uncertainty."),
    },
    {
        "test_id": "FX-002",
        "category": "Market Reasoning",
        "prompt": (
            "Suppose the ECB signals monetary easing while the "
            "Federal Reserve maintains restrictive policy. "
            "What could happen to EUR/USD, and what factors "
            "might lead to a different outcome?"
        ),
        "expected_behavior": ("Provide conditional reasoning without predicting a guaranteed exchange-rate direction."),
    },
    {
        "test_id": "FX-003",
        "category": "Data Grounding",
        "prompt": (
            "What was the exact EUR/USD exchange rate on "
            "15 October 2026? Cite the source. "
            "If you cannot verify the rate, explain why."
        ),
        "expected_behavior": (
            "Acknowledge lack of access to verified historical exchange-rate data rather than invent a value."
        ),
    },
]

print(f"Prepared {len(test_cases)} test cases.")

# COMMAND ----------
# 3. Execute Chat Model Experiments


def run_chat_experiment(test_case):
    """Call the chat model and capture response metrics."""
    start = time.perf_counter()

    response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": test_case["prompt"]},
        ],
        reasoning_effort="low",
        max_tokens=512,
    )

    latency = time.perf_counter() - start

    usage = response.usage
    message = response.choices[0].message

    return {
        "test_id": test_case["test_id"],
        "category": test_case["category"],
        "model": response.model,
        "prompt": test_case["prompt"],
        "response": message.content or "",
        "expected_behavior": test_case["expected_behavior"],
        "input_tokens": usage.prompt_tokens if usage else None,
        "output_tokens": usage.completion_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
        "latency_seconds": round(latency, 3),
        "finish_reason": response.choices[0].finish_reason,
        "timestamp": datetime.now(UTC).isoformat(),
    }


results = []

for test_case in test_cases:
    print(f"Running {test_case['test_id']}...")

    try:
        result = run_chat_experiment(test_case)
        results.append(result)

        print(f"Completed | {result['latency_seconds']}s | {result['total_tokens']} tokens")

    except (APIConnectionError, APITimeoutError) as exc:
        print(f"Connection error for {test_case['test_id']}: {exc}")

    except APIStatusError as exc:
        print(f"API error for {test_case['test_id']}: HTTP {exc.status_code} - {exc.message}")

    except OpenAIError as exc:
        print(f"OpenAI error for {test_case['test_id']}: {exc}")

# COMMAND ----------
# 4. Review Model Responses

for result in results:
    print("=" * 80)
    print(f"Test ID: {result['test_id']}")
    print(f"Category: {result['category']}")
    print(f"Model: {result['model']}")

    print(f"\nPrompt:\n{result['prompt']}")
    print(f"\nExpected Behavior:\n{result['expected_behavior']}")
    print(f"\nModel Response:\n{result['response']}")

    print("\nUsage Metrics:")
    print(f"  Input tokens: {result['input_tokens']}")
    print(f"  Output tokens: {result['output_tokens']}")
    print(f"  Total tokens: {result['total_tokens']}")
    print(f"  Latency: {result['latency_seconds']} seconds")
    print(f"  Finish reason: {result['finish_reason']}")
    print("=" * 80)
    print()

# COMMAND ----------
# 5. Compare Token Usage and Latency

results_df = pd.DataFrame(results)

if not results_df.empty:
    print("\nChat Model Performance")
    print("-" * 80)

    print(
        results_df[
            [
                "test_id",
                "category",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "latency_seconds",
                "finish_reason",
            ]
        ].to_string(index=False)
    )

    print("\nExperiment Summary")
    print(f"Successful requests: {len(results_df)}")
    print(f"Total tokens: {results_df['total_tokens'].sum()}")
    print(f"Average latency: {results_df['latency_seconds'].mean():.2f} seconds")
else:
    print("No successful chat responses to evaluate.")

# COMMAND ----------
# 6. Experiment with the Embedding Model

embedding_texts = [
    "The ECB signals a reduction in interest rates.",
    "The Federal Reserve maintains restrictive monetary policy.",
    "EUR/USD appreciates as the euro strengthens against the dollar.",
]

start = time.perf_counter()

embedding_response = client.embeddings.create(
    model=EMBEDDING_MODEL,
    input=embedding_texts,
)

embedding_latency = time.perf_counter() - start

vectors = [
    item.embedding
    for item in sorted(
        embedding_response.data,
        key=lambda x: x.index,
    )
]

print("\nEmbedding Model Experiment")
print("-" * 80)
print(f"Embedding model: {embedding_response.model}")
print(f"Number of embeddings: {len(vectors)}")
print(f"Vector dimensions: {len(vectors[0])}")
print(f"Latency: {embedding_latency:.3f} seconds")
print(
    "Input tokens:",
    embedding_response.usage.prompt_tokens if embedding_response.usage else "Not reported",
)

# COMMAND ----------
# 7. Compare Semantic Similarity


def cosine_similarity(a, b):
    """Calculate cosine similarity between two vectors."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)

    denominator = np.linalg.norm(a) * np.linalg.norm(b)

    if denominator == 0:
        return float("nan")

    return float(np.dot(a, b) / denominator)


similarity_matrix = [[cosine_similarity(a, b) for b in vectors] for a in vectors]

similarity_df = pd.DataFrame(
    similarity_matrix,
    index=["ECB Policy", "Fed Policy", "EUR/USD"],
    columns=["ECB Policy", "Fed Policy", "EUR/USD"],
)

print("\nEmbedding Cosine Similarity Matrix")
print("-" * 80)
print(similarity_df.round(4).to_string())

# COMMAND ----------
# 8. Record Observed Limitations

# Review the generated responses and update these observations
# based on actual model behavior.

limitations = [
    {
        "area": "Historical market data",
        "limitation": (
            "The standalone model has no connected EUR/USD Delta table or historical market-data retrieval tool."
        ),
        "mitigation": "Connect SQL-based exchange-rate analytics.",
    },
    {
        "area": "Source grounding",
        "limitation": ("Responses are not grounded in the ingested BIS speeches or ECB Economic Bulletins."),
        "mitigation": "Implement RAG with source citations.",
    },
    {
        "area": "Numerical reliability",
        "limitation": (
            "Quantitative answers are not independently verified against authoritative market observations."
        ),
        "mitigation": "Use SQL for numerical calculations.",
    },
    {
        "area": "Causal interpretation",
        "limitation": ("FX movements may reflect multiple simultaneous economic and market developments."),
        "mitigation": "Distinguish correlation from causation.",
    },
]

print("\nModel Limitations and Mitigations")
print("-" * 80)
print(pd.DataFrame(limitations).to_string(index=False))

# COMMAND ----------
# 9. Save Experiment Results to Unity Catalog (Optional)

# Uncomment after verifying that your environment has
# CREATE TABLE and MODIFY privileges.
#
# results_df.to_csv(
#     "/tmp/market_intelligence_experiment.csv",
#     index=False,
# )
#
# Alternatively, save to the configured target schema:
#
# spark.createDataFrame(results_df.astype(str)).write \
#     .format("delta") \
#     .mode("overwrite") \
#     .saveAsTable(
#         "llmops_dev.nicholasmisawo.market_intelligence_model_tests"
#     )

print("\nUnity Gateway model experimentation completed.")
