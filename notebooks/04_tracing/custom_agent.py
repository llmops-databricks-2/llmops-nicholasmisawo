# Databricks notebook source
# MAGIC %md
# MAGIC # 4.2 The FCR agent, traced
# MAGIC
# MAGIC The course's notebook 4.2 for this project. Run 4.0 first. The agent's traces go to its own
# MAGIC experiment (`experiment_path`), bound to the `llmops_*` trace tables.
# MAGIC
# MAGIC `FCRAgent` (`src/fcr_curator/agent.py`) is a ResponsesAgent, as the course's `ArxivAgent` is.
# MAGIC `FCRAgent.from_config` builds its tools as `ArxivAgent`'s constructor does:
# MAGIC - The Lakebase pgvector tools: `search_fcr` (hybrid search) and `extract_fcr_content`.
# MAGIC - The `get_fcr_details` UC function and Genie, both through managed MCP.
# MAGIC - Lakebase session memory.
# MAGIC
# MAGIC ## 1. What one turn's trace looks like
# MAGIC
# MAGIC ```
# MAGIC predict                      AGENT     traced by ResponsesAgent itself
# MAGIC └─ predict_stream            AGENT     reads session_id and request_id from custom_inputs
# MAGIC    ├─ memory_load            MEMORY    the session's last messages, from Lakebase
# MAGIC    └─ call_and_run_tools     CHAIN     tags the trace: session, request id, git_sha, deployment, version
# MAGIC       ├─ call_llm            LLM       with the gateway's token usage
# MAGIC       ├─ search_fcr          TOOL      a span per tool call, named after the tool
# MAGIC       │  └─ hybrid_search    RETRIEVER the passages found, as documents
# MAGIC       ├─ call_llm            LLM       ... until the model answers
# MAGIC       └─ memory_save         MEMORY    the question and answer
# MAGIC ```
# MAGIC
# MAGIC Where it differs from the course's agent, and why:
# MAGIC
# MAGIC | | Course | Ours | Why |
# MAGIC |---|---|---|---|
# MAGIC | Model call | not traced | `LLM` span with token usage | The notes flag the gap: no usage, no LLM breakdown |
# MAGIC | Memory spans | `RETRIEVER`, `CHAIN` | `MEMORY` | Retrieval judges read `RETRIEVER` spans as evidence |
# MAGIC | Tool span name | `execute_tool` | the tool's name | The tree reads `search_fcr`, and spans filter by tool |
# MAGIC | Retrieval | inside the tool span | its own `RETRIEVER` span | Groundedness checks (4.3) need the passages as documents |
# MAGIC | Loop end | "Max iterations reached" | one answer without tools | The notes call the course's an incomplete task |
# MAGIC | Tool calls per round | the first | all of them | The model often asks for several pages at once |

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build with the system's libpq: its binary build aborted Python
# on serverless compute (docs/08-agent.md). Set before anything imports psycopg.

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

from uuid import uuid4

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import display
from loguru import logger
from mlflow.entities import SpanStatusCode, SpanType, Trace
from mlflow.types.responses import ResponsesAgentRequest

from fcr_curator.agent import FCRAgent, response_text
from fcr_curator.config import get_widget, load_config
from fcr_curator.mcp import DEV_GENIE_SPACE_ID
from fcr_curator.tracing import use_experiment

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
GENIE_SPACE_ID = get_widget("genie_space_id", DEV_GENIE_SPACE_ID)
os.environ["GIT_SHA"] = get_widget("git_sha", "local")  # The job passes the bundle's commit
cfg = load_config("../../project_config.yml", env=ENV)
experiment = use_experiment(cfg, cfg.experiment_name)
logger.info(f"Tracing to {experiment.name} (id={experiment.experiment_id})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Build the agent

# COMMAND ----------

agent = FCRAgent.from_config(cfg, ENV, GENIE_SPACE_ID, WorkspaceClient())
logger.info(f"Tools: {list(agent.loop.tools_by_name)}")


def ask(question: str, session_id: str) -> str:
    """Send one question in the session, with a new request id, and return the answer's text."""
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": question}],
        custom_inputs={"session_id": session_id, "request_id": f"req-{uuid4()}"},
    )
    return response_text(agent.predict(request))


def traces_of(session_id: str) -> list[Trace]:
    """Return the session's traces, oldest first, found by the session id the agent puts in their metadata."""
    return mlflow.search_traces(
        locations=[experiment.experiment_id],
        filter_string=f"request_metadata.`mlflow.trace.session` = '{session_id}'",
        order_by=["timestamp_ms ASC"],
        return_type="list",
        flush=True,
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. One traced request

# COMMAND ----------

single_session = f"s-{uuid4()}"
answer = ask("How did Wilton Re's solvency compare with its enhanced capital requirement in 2024?", single_session)
logger.info(f"Session {single_session}\n{answer}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. A conversation: two turns of one session
# MAGIC
# MAGIC Each turn sends only its new question. The agent loads the session's history from Lakebase, so
# MAGIC replaying the earlier turns in the request as well would duplicate them, which the notes warn about.
# MAGIC The second question only makes sense after the first.

# COMMAND ----------

conversation = f"s-{uuid4()}"
for question in (
    "How many Class 4 insurers have an FCR for 2024?",
    "List their names.",  # "Their" only makes sense with the first turn loaded from memory
):
    logger.info(f"Q: {question}\nA: {ask(question, conversation)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. The session's traces
# MAGIC
# MAGIC One trace per request, found by the session id the agent put in their metadata.

# COMMAND ----------

session_traces = traces_of(conversation)
logger.info(f"{len(session_traces)} traces in session {conversation}")
display(
    [
        {
            "request_id": trace.info.client_request_id,
            "state": str(trace.info.state),
            "duration_ms": trace.info.execution_duration,
            "tokens": (trace.info.token_usage or {}).get("total_tokens"),
            "git_sha": trace.info.tags.get("git_sha"),
        }
        for trace in session_traces
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Where the time goes
# MAGIC
# MAGIC Across the session's traces: time and calls by span type. A slow turn usually shows one span type
# MAGIC dominating: retrieval, repeated model calls, or a tool retried.

# COMMAND ----------

by_type: dict[str, list[float]] = {}
for trace in session_traces:
    for span in trace.data.spans:
        by_type.setdefault(str(span.span_type), []).append((span.end_time_ns - span.start_time_ns) / 1e6)
display(
    [
        {"span_type": span_type, "spans": len(durations), "total_ms": round(sum(durations))}
        for span_type, durations in sorted(by_type.items(), key=lambda item: -sum(item[1]))
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. One trace in detail
# MAGIC
# MAGIC The second turn: its span tree, each tool's arguments, and the model calls' token usage.

# COMMAND ----------

trace = session_traces[-1]
names = {span.span_id: span.name for span in trace.data.spans}
logger.info(f"Trace {trace.info.trace_id}: {trace.info.state}, {trace.info.execution_duration} ms")
logger.info(f"Metadata: session {trace.info.trace_metadata.get('mlflow.trace.session')}")
logger.info(f"Token usage: {trace.info.token_usage}")
for span in trace.data.spans:
    duration_ms = (span.end_time_ns - span.start_time_ns) / 1e6
    detail = f" {span.inputs}" if str(span.span_type) == "TOOL" else ""
    logger.info(
        f"  {span.name} [{span.span_type}] under {names.get(span.parent_id, '-')}: {duration_ms:.0f} ms{detail}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. An edge case: a real insurer the tools refuse
# MAGIC
# MAGIC "AXA" is a Bermuda insurer's real short name (AXA XL), but the tools refuse names under four
# MAGIC letters, since short names match several companies. The refusal goes back to the model as an error
# MAGIC it can act on. The trace shows which step failed and why: the tool span is marked as an error, with
# MAGIC the exception's message, while the turn itself still succeeds.

# COMMAND ----------

edge_session = f"s-{uuid4()}"
edge_answer = ask("Which FCRs has AXA published?", edge_session)
[edge_trace] = traces_of(edge_session)
logger.info(f"Trace {edge_trace.info.trace_id}: {edge_trace.info.state}\nAnswer: {edge_answer}")

tool_calls = [span for span in edge_trace.data.spans if span.span_type == SpanType.TOOL]
for span in tool_calls:
    errors = [event.attributes.get("exception.message") for event in span.events if event.name == "exception"]
    logger.info(f"  {span.name}({span.inputs}) -> {span.status.status_code.value} {errors or ''}")
if not any(span.status.status_code == SpanStatusCode.ERROR for span in tool_calls):
    logger.warning("No tool refused a call this time: the model chose another route (read the tool spans above)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Is the UC function slow, or was the warehouse cold?
# MAGIC
# MAGIC `get_fcr_details` took 38 to 47 seconds in earlier runs, each the first call in days. The same question
# MAGIC twice in a row tells the two apart: if the second call is fast, the first was waiting for compute.

# COMMAND ----------

details_seconds = []
for attempt in (1, 2):
    attempt_session = f"s-{uuid4()}"
    ask("List the FCRs Wilton Re has published.", attempt_session)
    [attempt_trace] = traces_of(attempt_session)
    details = [span for span in attempt_trace.data.spans if span.name == "get_fcr_details"]
    seconds = [round((span.end_time_ns - span.start_time_ns) / 1e9, 1) for span in details]
    details_seconds.append(seconds)
    logger.info(f"Attempt {attempt}: get_fcr_details took {seconds or 'no call'} s")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. What to check in a trace
# MAGIC
# MAGIC | Symptom | Evidence in the trace |
# MAGIC |---|---|
# MAGIC | Slow answer | Which span type dominates (section 6): retrieval, repeated model calls, retries |
# MAGIC | Unsupported figure | The `RETRIEVER` and `extract_fcr_content` outputs: did the model receive that figure? |
# MAGIC | Wrong follow-up | `memory_load`'s output: was the session's history loaded, and only once? |
# MAGIC | High token use | The `LLM` spans' usage, and how many rounds the model took |
# MAGIC | No answer | Did the rounds run out, a tool fail (an error span), or the model reply empty? |
# MAGIC
# MAGIC A successful trace isn't a correct answer: the turn can finish normally with an unsupported figure.
# MAGIC Evaluation (4.3) checks the answers themselves.
