# Databricks notebook source
# MAGIC %md
# MAGIC # 4.1 MLflow tracing, with FCR examples
# MAGIC
# MAGIC The course's notebook 4.1, section by section, on this project's domain. Run 4.0 first.
# MAGIC
# MAGIC These are teaching traces, so they go to the **demo experiment** (`demo_experiment_path`, dev only)
# MAGIC and its own `llmops_demo_*` tables, apart from the agent's real traces (4.2). Each span gets the type
# MAGIC that says what it does: `TOOL`, `LLM`, `CHAIN`, `AGENT` or `RETRIEVER`.
# MAGIC
# MAGIC | Section | Shows |
# MAGIC |---|---|
# MAGIC | 1–3 | `@mlflow.trace`, span types, and manual spans |
# MAGIC | 4 | A failing span, and the error it records |
# MAGIC | 5–6 | Session, request and release metadata, and searching by it |
# MAGIC | 7 | A real model call through the gateway, with its token usage |
# MAGIC | 8 | A whole agent turn, simulated |
# MAGIC | 9–12 | Reading traces back, do's and don'ts, and filters |
# MAGIC
# MAGIC Unlike the course, searches return Trace objects (`return_type="list"`), not a DataFrame whose
# MAGIC columns change between MLflow versions, so no column guards are needed.

# COMMAND ----------

import os
import time
from datetime import datetime
from uuid import uuid4

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import display
from loguru import logger
from mlflow.entities import SpanType

from fcr_curator.config import get_widget, load_config
from fcr_curator.gateway import gateway_client
from fcr_curator.tracing import record_token_usage, release_tags, use_experiment

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
os.environ["GIT_SHA"] = get_widget("git_sha", "local")  # The job passes the bundle's commit
cfg = load_config("../../project_config.yml", env=ENV)
if not cfg.demo_experiment_path:
    raise ValueError(f"The tracing demos run in dev; {ENV} has no demo_experiment_path in project_config.yml")
experiment = use_experiment(cfg, cfg.demo_experiment_path)
logger.info(f"Tracing to {experiment.name} (id={experiment.experiment_id})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Simple tracing with `@mlflow.trace`
# MAGIC
# MAGIC The decorator records the call's inputs, output, timing and status as one span, which is one trace
# MAGIC when nothing else is being traced.

# COMMAND ----------


@mlflow.trace
def solvency_ratio(eligible_capital: float, ecr: float) -> float:
    """Return eligible capital over the enhanced capital requirement: how FCRs' ratios are worked out."""
    return round(eligible_capital / ecr, 2)


ratio = solvency_ratio(1757.3, 717.1)  # Wilton Re, 2024 (docs/08-agent.md)
logger.info(f"Solvency ratio: {ratio}; trace {mlflow.get_last_active_trace_id()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Span types
# MAGIC
# MAGIC A function called inside a traced one becomes its child span: the parent comes from the active
# MAGIC tracing context, not from the names. Types let the UI, and later the evaluation judges, read what each
# MAGIC step was.

# COMMAND ----------


@mlflow.trace(span_type=SpanType.RETRIEVER)
def find_fcr_pages(insurer: str, reporting_year: int) -> list[dict]:
    """Stand in for search_fcr's retrieval: the pages that report the insurer's capital."""
    return [
        {"insurer": insurer, "reporting_year": reporting_year, "page": 15, "text": "Total eligible capital 1,757.3"},
        {
            "insurer": insurer,
            "reporting_year": reporting_year,
            "page": 16,
            "text": "Enhanced capital requirement 717.1",
        },
    ]


@mlflow.trace(span_type=SpanType.LLM)
def draft_answer(prompt: str) -> str:
    """Stand in for the model call."""
    return "Eligible capital was 1,757.3 against an ECR of 717.1, about 2.45 times."


@mlflow.trace(span_type=SpanType.CHAIN)
def answer_question(question: str, insurer: str, reporting_year: int) -> str:
    """Retrieve the pages, then answer from them: one parent CHAIN span with two children."""
    pages = find_fcr_pages(insurer, reporting_year)
    evidence = "\n".join(page["text"] for page in pages)
    return draft_answer(f"Question: {question}\nEvidence:\n{evidence}")


answer = answer_question("How did Wilton Re's capital compare with its ECR?", "Wilton Re", 2024)
logger.info(f"Answer: {answer}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Manual spans
# MAGIC
# MAGIC `mlflow.start_span` when a decorator won't do: a step that isn't its own function, or a span named at
# MAGIC run time. Set its inputs and outputs yourself, or it records only a name and a duration.

# COMMAND ----------


def compare_years(insurer: str, figures: dict[int, tuple[float, float]]) -> float:
    """Return the change in the solvency ratio between the first and last year, a span per year."""
    with mlflow.start_span(name="compare_years", span_type=SpanType.CHAIN) as span:
        span.set_inputs({"insurer": insurer, "years": sorted(figures)})
        ratios = {}
        for year, (eligible_capital, ecr) in sorted(figures.items()):
            with mlflow.start_span(name=f"ratio_{year}") as year_span:
                year_span.set_inputs({"eligible_capital": eligible_capital, "ecr": ecr})
                ratios[year] = round(eligible_capital / ecr, 2)
                year_span.set_outputs({"ratio": ratios[year]})
        change = round(ratios[max(ratios)] - ratios[min(ratios)], 2)
        span.set_outputs({"ratios": ratios, "change": change})
        return change


change = compare_years("Wilton Re", {2023: (1547.0, 545.2), 2024: (1757.3, 717.1)})
logger.info(f"Change in the ratio, 2023 to 2024: {change}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. A failing span
# MAGIC
# MAGIC An exception inside a span marks it, and its trace, as an error, with the exception recorded as an
# MAGIC event. That's the evidence to read when a request fails (section 12 finds these traces).

# COMMAND ----------


@mlflow.trace(span_type=SpanType.TOOL)
def get_fcr_details(insurer: str) -> str:
    """Stand in for the get_fcr_details tool, which refuses a name too short to match one insurer."""
    if len(insurer) < 4:
        raise ValueError(f"insurer {insurer!r} is too short to match one company; give more of its name")
    return '[{"reporting_year": 2024, "fcr_class": "Long Term Class E"}]'


try:
    get_fcr_details("Re")
except ValueError as error:
    logger.info(f"Traced as an error: {error}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Metadata and tags
# MAGIC
# MAGIC A session groups a conversation's requests; each request is its own trace with its own id. Release
# MAGIC tags (`git_sha`, deployment, model version) let traces be compared across versions. `release_tags()`
# MAGIC reads them from the environment as the agent does; "local" outside a deployment.

# COMMAND ----------

session_id = f"demo-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
request_id = f"req-{uuid4()}"


@mlflow.trace(span_type=SpanType.CHAIN)
def tagged_lookup(insurer: str) -> str:
    """Look an insurer up, tagging the trace with its session, request and release."""
    mlflow.update_current_trace(
        metadata={"mlflow.trace.session": session_id},
        tags={**release_tags(), "request_type": "lookup"},
        client_request_id=request_id,
    )
    return get_fcr_details(insurer)


tagged_lookup("Wilton Re")
logger.info(f"Session {session_id}, request {request_id}, release {release_tags()}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Searching traces
# MAGIC
# MAGIC Traces are written in the background; `flush=True` waits for this process's pending ones. Searching
# MAGIC traces kept in Unity Catalog runs SQL on the warehouse that `use_experiment` set
# MAGIC (`MLFLOW_TRACING_SQL_WAREHOUSE_ID`).

# COMMAND ----------

git_sha = release_tags()["git_sha"]
tagged = mlflow.search_traces(
    locations=[experiment.experiment_id],
    filter_string=f"tags.git_sha = '{git_sha}'",
    max_results=5,
    return_type="list",
    flush=True,
)
logger.info(f"{len(tagged)} traces tagged git_sha={git_sha}")
display([{"trace_id": trace.info.trace_id, "state": str(trace.info.state)} for trace in tagged])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. A real model call
# MAGIC
# MAGIC A custom call isn't instrumented by any autolog, so the LLM span records the gateway's token usage
# MAGIC itself (`record_token_usage`). The notes point out the course's `call_llm` leaves this out. A reply
# MAGIC without usage records nothing, so it can't be read as zero.

# COMMAND ----------

client = gateway_client(WorkspaceClient())


@mlflow.trace(span_type=SpanType.LLM)
def ask_model(question: str) -> str:
    """Ask the gateway's chat model one question, recording its token usage on this span."""
    response = client.chat.completions.create(
        model=cfg.llm_endpoint,
        messages=[
            {"role": "system", "content": "You explain Bermuda insurance regulation in one sentence."},
            {"role": "user", "content": question},
        ],
        max_tokens=500,
    )
    record_token_usage(response.usage)
    return response.choices[0].message.content


logger.info(ask_model("What is the BMA's enhanced capital requirement?"))
logger.info(f"Token usage: {mlflow.get_trace(mlflow.get_last_active_trace_id(), flush=True).info.token_usage}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. A whole agent turn, simulated
# MAGIC
# MAGIC The shape 4.2's real agent leaves: an `AGENT` span tagged with the session, then a step per decision.

# COMMAND ----------


@mlflow.trace(span_type=SpanType.AGENT)
def agent_turn(question: str) -> dict:
    """Simulate one agent turn: work out the request, search, answer."""
    turn_session = f"demo-{uuid4()}"
    turn_request = f"req-{uuid4()}"
    mlflow.update_current_trace(
        metadata={"mlflow.trace.session": turn_session},
        tags={**release_tags(), "agent": "fcr_curator"},
        client_request_id=turn_request,
    )
    with mlflow.start_span("analyse_question", span_type=SpanType.CHAIN) as span:
        span.set_inputs({"question": question})
        request = {"insurer": "Wilton Re", "reporting_year": 2024, "measure": "eligible capital and ECR"}
        span.set_outputs(request)
    with mlflow.start_span("search_fcr", span_type=SpanType.TOOL) as span:
        span.set_inputs({"query": request["measure"], "insurer": request["insurer"], "reporting_year": 2024})
        pages = find_fcr_pages(request["insurer"], request["reporting_year"])
        span.set_outputs({"pages": pages})
    with mlflow.start_span("generate_answer", span_type=SpanType.LLM) as span:
        span.set_inputs({"question": question, "pages": pages})
        reply = f"From {len(pages)} pages of its 2024 FCR: eligible capital 1,757.3 against an ECR of 717.1."
        span.set_outputs({"answer": reply})
    return {"answer": reply, "session_id": turn_session, "request_id": turn_request}


turn = agent_turn("How did Wilton Re's solvency compare with its ECR in 2024?")
logger.info(f"{turn['answer']} (session {turn['session_id']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Reading traces back

# COMMAND ----------

recent = mlflow.search_traces(
    locations=[experiment.experiment_id], order_by=["timestamp_ms DESC"], max_results=10, return_type="list", flush=True
)
display(
    [
        {
            "trace_id": trace.info.trace_id,
            "state": str(trace.info.state),
            "duration_ms": trace.info.execution_duration,
            "root_span": trace.data.spans[0].name if trace.data.spans else None,
        }
        for trace in recent
    ]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. One trace in detail: its tags, metadata and span tree
# MAGIC
# MAGIC The most recent trace, section 8's agent turn.

# COMMAND ----------

trace = recent[0]
logger.info(f"Trace {trace.info.trace_id}: {trace.info.state}, {trace.info.execution_duration} ms")
logger.info(f"Tags: { {key: value for key, value in trace.info.tags.items() if not key.startswith('mlflow.')} }")
logger.info(f"Session: {trace.info.trace_metadata.get('mlflow.trace.session')}")
names = {span.span_id: span.name for span in trace.data.spans}
for span in trace.data.spans:
    duration_ms = (span.end_time_ns - span.start_time_ns) / 1e6
    logger.info(f"  {span.name} [{span.span_type}] under {names.get(span.parent_id, '-')}: {duration_ms:.1f} ms")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 11. Do's and don'ts
# MAGIC
# MAGIC **Do** (the course's list, with the lecture notes' additions):
# MAGIC 1. Give each span the type that says what it does: `AGENT`, `CHAIN`, `LLM`, `TOOL`, `RETRIEVER`,
# MAGIC    `MEMORY`. Judges read them: a retrieval judge reads `RETRIEVER` spans, so don't label memory one.
# MAGIC 2. Tag each trace with its session and request ids, and its release: `git_sha`, deployment, model version.
# MAGIC 3. Set inputs and outputs on every manual span.
# MAGIC 4. Trace every model call, and record its token usage: missing usage isn't zero.
# MAGIC 5. Let errors mark their spans; read the span's status and events when a request fails.
# MAGIC 6. Keep the evidence a later check needs: a groundedness judge can't verify a claim against passages
# MAGIC    that were never recorded.
# MAGIC 7. Grant the identity that writes traces explicitly, and test that identity, not just your own.
# MAGIC
# MAGIC **Don't**:
# MAGIC 1. Trace too finely: string formatting, parsing and small helpers add spans without evidence.
# MAGIC 2. Record secrets, or personal data the diagnosis doesn't need.
# MAGIC 3. Create traces without context: a trace no one can find by session, request or release is noise.
# MAGIC 4. Mix teaching or test traces into the application's experiment (hence this demo experiment).
# MAGIC 5. Take a successful trace for a correct answer: that's what evaluation (4.3) is for.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 12. Filtering traces

# COMMAND ----------

query = {"locations": [experiment.experiment_id], "max_results": 5, "return_type": "list"}
failed = mlflow.search_traces(filter_string="status = 'ERROR'", **query)
logger.info(f"Failed traces: {len(failed)} (section 4's is one)")

agent_turns = mlflow.search_traces(filter_string="tags.agent = 'fcr_curator'", **query)
logger.info(f"Simulated agent turns: {len(agent_turns)}")

last_hour_ms = int((time.time() - 3600) * 1000)
last_hour = mlflow.search_traces(filter_string=f"timestamp_ms > {last_hour_ms}", **query)
logger.info(f"Traces in the last hour (first 5): {len(last_hour)}")
