# 09 Tracing

Every agent request leaves a trace in MLflow: one tree of spans that shows what was loaded, which tools
the model called with what arguments, what they returned, and how long each step took. This is lecture 4's
4.0 to 4.2 for this project: trace storage in Unity Catalog, the tracing APIs, and the traced agent.
Evaluation (4.3) and logging the agent as a model (4.4) come later.

| Course notebook | Here | Does |
|---|---|---|
| 4.0 `uc_traces_setup` | [`notebooks/04_tracing/uc_traces_setup.py`](../notebooks/04_tracing/uc_traces_setup.py) | Binds the experiments to their trace tables; grants the deploy service principal write access |
| 4.1 `tracing_implementation` | [`notebooks/04_tracing/tracing_implementation.py`](../notebooks/04_tracing/tracing_implementation.py) | The tracing APIs, with FCR examples, traced to a demo experiment |
| 4.2 `custom_agent` | [`notebooks/04_tracing/custom_agent.py`](../notebooks/04_tracing/custom_agent.py) | The FCR agent, traced: one request, a two-turn session, its traces read back |

The `fcr_tracing` job runs them in order, each needing the one before:

```bash
databricks bundle run fcr_tracing -t dev
```

## Where traces live

Traces hold whole questions, retrieved FCR passages and answers, so they're kept in Unity Catalog tables,
where UC privileges govern who reads them, not workspace experiment permissions. An experiment is the
interface to them.

| Experiment | Traces | Tables in `<catalog>.<schema>` |
|---|---|---|
| `/llmops/nicholasmisawo-<env>` (`experiment_path`) | The agent's | `llmops_otel_{spans,logs,metrics,annotations}`, views `llmops_trace_{metadata,unified}` |
| `~/nicholasmisawo-dev-demos` (`demo_experiment_path`, dev only), in your workspace home | 4.1's teaching examples | `llmops_demo_*`, the same set |

The demos have their own experiment **and** their own tables. An experiment name alone isolates nothing:
two experiments sharing tables share every query over them. Kept apart, the agent's traces, which
evaluation will read, hold only real agent requests.

The course bound the agent experiments at onboarding. 4.0 checks each binding and stops if an experiment
is bound elsewhere, rather than converting it. It creates the demo experiment the first time, in your
workspace home: only the course can create experiments in `/llmops`, which the first run found
(`PERMISSION_DENIED` on the folder). `~` keeps your username out of the repository.

The settings are in [`project_config.yml`](../project_config.yml): `experiment_path` and
`demo_experiment_path` per environment, and a shared `tracing` section with the table prefixes and the
secret naming the trace writer.

## Who can write them

| Identity | Needs | Set by |
|---|---|---|
| You, running notebooks | `SELECT, MODIFY` on the trace tables | The course, at onboarding |
| `sp-deploy-nicholasmisawo-<env>`, the deployed agent | Explicit `SELECT, MODIFY` on the tables, `SELECT` on the views | 4.0 |
| Both | `USE CATALOG`, `USE SCHEMA`; `CAN USE` on the warehouse to search traces | The course (not readable without admin rights) |

Before 4.0 ran, the service principal held only `ALL PRIVILEGES` inherited from the schema. Databricks
requires the explicit grants on trace tables, so a notebook test passing as you wouldn't have shown the
deployed agent could write traces. 4.0 reads the principal's application id from the UC secret
`<catalog>.<schema>.databricks_client_id`, grants, then reads the grants back and fails if any is missing.

## What a turn's trace looks like

```text
predict                      AGENT      traced by MLflow's ResponsesAgent itself
└─ predict_stream            AGENT      reads session_id and request_id from custom_inputs
   ├─ memory_load            MEMORY     the session's last 20 messages, from Lakebase
   └─ call_and_run_tools     CHAIN      tags the trace with the session, request id and release
      ├─ call_llm            LLM        with the gateway's token usage
      ├─ search_fcr          TOOL       one span per tool call, named after the tool
      │  └─ hybrid_search    RETRIEVER  the passages found, as MLflow documents
      ├─ extract_fcr_content TOOL
      ├─ call_llm            LLM        ... until the model answers
      └─ memory_save         MEMORY     the question and answer
```

Each trace carries:

| Field | Value | Used to |
|---|---|---|
| `mlflow.trace.session` (metadata) | The caller's `session_id` | Find a conversation's traces together |
| `client_request_id` | The caller's `request_id` | Find one request |
| `git_sha` (tag) | The bundle's commit in the job, "local" from a laptop | Compare behaviour across versions |
| `deployment_name`, `model_version` (tags) | The App or endpoint and version once deployed, "local" until then | The same, after lecture 5 |

## Ours and the course's

The agent follows the course's `ArxivAgent`: an MLflow `ResponsesAgent`, built from configuration, with
the course's methods and span layout. That's what 4.4 needs to log and serve it. Where it differs, ours
is kept for a reason:

| | Course | Ours | Why |
|---|---|---|---|
| Model call | Not traced | `LLM` span with token usage | The lecture notes flag the gap: no usage, no breakdown of model calls. Missing usage is left missing, never recorded as zero |
| Memory spans | `RETRIEVER` (load), `CHAIN` (save) | `MEMORY` | Retrieval judges read `RETRIEVER` spans as evidence, so they'd judge answers against chat history |
| Tool span | `execute_tool` | The tool's name | The tree reads `search_fcr`, and spans can be filtered by tool |
| Retrieval | Inside the tool span | Its own `RETRIEVER` span, passages as documents | Groundedness checks (4.3) read retrieved documents from `RETRIEVER` spans |
| Loop | One tool call per model turn; ends "Max iterations reached" | Every tool call in a round; a final answer without tools | The notes call the course's ending an incomplete task |
| Tool errors | Raised | Returned to the model | The model corrects a bad call, as lecture 3 found |
| Rate limits | Retried forever (`backoff.expo`) | At most 3 tries, a minute apart | A request fails rather than hanging |
| Construction | Everything in `__init__` | `from_config` builds the parts; the constructor takes them | Tests pass fakes; 4.4 calls `from_config` the same way |
| Grants | Every `llmops_` object | Each prefix's `_otel_` and `_trace_` objects, then checked | `llmops_` would also match the demos' tables |
| Grant method | `spark.sql` GRANT strings | `workspace.grants.update` | Idempotent, no SQL quoting, and it runs from a laptop too |
| `git_sha` | Hard-coded in the job | `${bundle.git.commit}` | Each trace names the code that produced it |
| Trace search | DataFrame, with column guards | `return_type="list"`, `flush=True` | Trace objects keep their fields across MLflow versions; flushing avoids reading before the write |

Memory keeps lecture 3's choice too: only the question and answer are saved, not the tool calls.

## Do's and don'ts

From the course's notebook and lecture notes. The [`mlflow-tracing` skill](#the-mlflow-tracing-skill) has
them with examples.

**Do**

- Give each span the type that says what it does: `AGENT`, `CHAIN`, `LLM`, `TOOL`, `RETRIEVER`, `MEMORY`.
- Tag each trace with its session, request and release.
- Set inputs and outputs on every manual span.
- Trace every model call, and record its token usage.
- Let errors mark their spans, and read a failed request's span status and events.
- Keep the evidence a later check needs: a groundedness judge can't verify a claim against passages that
  were never recorded.
- Grant the identity that writes traces explicitly, and test that identity.

**Don't**

- Trace too finely: formatting, parsing and small helpers add spans without evidence.
- Record secrets, or personal data the diagnosis doesn't need.
- Create traces no one can find by session, request or release.
- Mix teaching or test traces into the application's experiment.
- Take a successful trace for a correct answer: the turn can finish normally with an unsupported figure.

## The mlflow-tracing skill

A workbench skill, `mlflow-tracing`, records how to trace both kinds of application this course covers:

- Regular ML: training and batch scoring.
- AI agents: span types, metadata, token usage and sessions.

It covers UC trace storage and grants, searching, troubleshooting, and the do's and don'ts above. It lives in
the workbench's `.claude/skills/` and `.agents/skills/`, not in this project.

## Results

Job run `461052584836630` (dev, commit `4da4a6b`) passed all three tasks. The first run failed in 4.0 with
`PERMISSION_DENIED`: only the course can create experiments in `/llmops`, so the demo experiment moved
to your workspace home.

| Check | Result |
|---|---|
| Agent experiment binding | Unchanged: `llmops_dev.nicholasmisawo`, prefix `llmops` |
| Demo experiment | Created, with its own `llmops_demo_otel_*` tables and `llmops_demo_trace_*` views |
| Service principal on the agent's trace tables | Explicit `SELECT, MODIFY` on the 4 tables and `SELECT` on the 2 views, read back after granting |
| Service principal on the demo tables | None, as intended: only the agent's traces are written by a deployment |
| Agent traces | Each with its session, request id, the real `git_sha` and token usage |
| Two-turn session | 2 traces found by session id; the follow-up ("List their names.") was answered from the loaded history |

The span tree of the Wilton Re request, as recorded:

| Span | Type | Time |
|---|---|---|
| `predict` > `predict_stream` | AGENT | 76.3 s |
| `memory_load` | MEMORY | 5.0 s |
| `get_fcr_details` | TOOL | **46.9 s** |
| `search_fcr` > `hybrid_search` | TOOL > RETRIEVER | 1.5 s |
| `extract_fcr_content` × 2 | TOOL | 0.3 s each |
| `query_space_…` (Genie) | TOOL | 13.9 s |
| `call_llm` × 6 | LLM | 0.9 to 2.2 s each; 25,405 tokens in all |
| `memory_save` | MEMORY | 0.3 s |

What the trace shows, which the answer alone didn't:

- **The UC function dominates.** `get_fcr_details` took 47 of the 76 seconds, against 1.5 seconds for
  the search. The warm-run test below shows a cold warehouse explains most of that, but not all of it.
- **An unneeded Genie call.** Asked about one insurer's capital, the model also asked Genie, adding 14
  seconds and a round. The tool guidance reserves Genie for counts and lists.
- **The first Lakebase connection costs 5 seconds** (`memory_load`), against 0.3 s for `memory_save` later.

These are leads for evaluation (4.3), not yet fixes.

### A failure, and the step that caused it

Run `916596661469909` (commit `cc7122c`) asked *"Which FCRs has AXA published?"* AXA is a real insurer
whose short name the tools refuse, since names under four letters match several companies. The trace
shows where the turn went wrong and how it recovered:

| # | Span | Status | What happened |
|---|---|---|---|
| 1 | `get_fcr_details {"insurer": "AXA"}` | **ERROR**, 0.0 s | *"insurer 'AXA' is too short to match one company; give more of its name"*, recorded as the span's exception |
| 2 | `search_fcr {"query": "AXA"}` | OK, 0.8 s | The model searched the FCR text instead |
| 3 | `query_space_…` (Genie) | OK, 28.4 s | It asked Genie for insurers with AXA in their name |
| 4 | `get_fcr_details {"insurer": "AXA Life Bermuda"}` | OK, 6.3 s | It retried with a full name Genie returned |

The turn itself finished OK (43.8 s). Only the refused tool call is marked as an error, so a search for
failed traces misses it: search the spans instead. The answer found two AXA entities, AXA Life Bermuda
Limited and AXA XL Reinsurance Ltd, and no FCRs recorded for either. The trace explains the cost: the
refusal took one wasted round and a 28-second Genie call to recover from. Telling the model to pass full
registered names (*AXA XL Reinsurance*), or letting the tool suggest matches, would avoid both.

The same answer says no FCRs were "recorded as published". That's closer than the earlier "not filed",
but the register still only knows what the pipeline found.

### Cold warehouse or slow function?

The same question, *"List the FCRs Wilton Re has published."*, asked twice in a row in that run:

| `get_fcr_details` call | Time |
|---|---|
| Earlier runs, the first call in days (warehouse idle) | 46.9 s and 38.1 s |
| This run's first call (warehouse already started) | 11.6 s |
| The question asked twice, back to back | 10.0 s, 9.9 s |

So a cold warehouse added roughly 30 seconds, but the warm call still takes about 10 seconds, against
under a second for `search_fcr`. Both the UC function's SQL and its route through managed MCP are worth
measuring separately. Genie took 28 to 29 seconds on every call in this run, so it's the slowest tool
once the warehouse is warm.
