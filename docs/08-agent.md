# 08 Agent

The FCR agent answers questions about Bermuda insurers' Financial Condition Reports with tools, and
keeps each conversation in Lakebase so a follow-up ("And in 2023?") is answered in context.

```bash
# Set up and check the tools, the Genie space and the session table (four tasks, in parallel)
databricks bundle deploy -t dev
databricks bundle run fcr_agent -t dev
# Then talk to the agent from your machine
env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/03_agent/fcr_agent.py
```

## Tools

Each technique the course covers is used once, where it fits best:

| Tool | Technique | Answers | Module |
|---|---|---|---|
| `search_fcr` | Lakebase pgvector | Which passages discuss a topic, filtered by insurer, year or licence class | `agent_tools.py`, `vector_search.py` |
| `get_fcr_details` | Unity Catalog function, through managed MCP | Which FCRs an insurer has: years, class, business, PDF links | `uc_functions.py` |
| `extract_fcr_content` | Custom Python function | The full text of one cited page, to quote its figures | `agent_tools.py`, `search.py` |
| Genie | Genie space, through managed MCP | Counts and lists across insurers ("Which Class 4 insurers lack a 2024 FCR?") | `resources/genie_space.yml` |

They don't overlap: search finds passages, details lists reports, extract reads a page, Genie aggregates.
Following the course's tool design practices:

- **Clear descriptions** tell the model when to use each tool and where its arguments come from
  (`extract_fcr_content` takes the `file_url` and `page` a `search_fcr` result gives).
- **Validated inputs**: a blank query, an unknown licence class, an implausible year, more than 10
  results, a name too short to match one insurer ("Re") or a URL with a `#page=` part are refused with a
  `ToolInputError` saying what to change.
- **Structured results**: every tool returns JSON; excerpts are capped at 800 characters and pages at
  12,000, so one result can't crowd out the rest of the turn.
- **Errors the model can act on**: the agent returns a rejected call's message to the model as
  `{"error": ...}`. Outages and bugs aren't caught: they fail the turn rather than hide.

`get_fcr_details` matches insurers as curation does: letters and digits only, legal suffixes dropped,
so "Wilton Re" and "Wilton Re Ltd." both find Wilton Reinsurance Bermuda Limited. The agent checks the
name before calling the function, and the function's SQL checks it again, since anyone with EXECUTE can
call it.

## Orchestration

One turn of a session. Green is memory, red the fallback, and each tool has its own colour; the model
calls only the tools a question needs (for the Wilton Re question: details, search, then two pages).

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Agent as FCRAgent<br/>(agent.py)
    participant Memory as Session memory<br/>(Lakebase)
    participant Model as Chat model<br/>(course_ops.gateway.chat)
    participant Details as get_fcr_details<br/>(UC function, MCP)
    participant Search as search_fcr<br/>(Lakebase pgvector)
    participant Extract as extract_fcr_content<br/>(custom function)
    participant Genie as Genie space<br/>(MCP)

    User->>Agent: question + session_id
    rect rgba(46, 160, 67, 0.15)
        Agent->>Memory: load the last 20 messages of the session
        Memory-->>Agent: history, oldest first
    end
    Agent->>Agent: system prompt + tool guidance + history + question

    loop up to 8 rounds of tool calls
        Agent->>Model: messages + tool specs
        alt the model calls tools
            Model-->>Agent: the tools it chose, with arguments
            alt which reports exist
                rect rgba(27, 169, 255, 0.15)
                    Agent->>Details: insurer
                    Details-->>Agent: FCRs by year, licence class, PDF link
                end
            else find passages
                rect rgba(130, 80, 223, 0.15)
                    Agent->>Search: query + insurer, year, class filters
                    Search-->>Agent: passages with file_url, page, citation_url
                end
            else read a page
                rect rgba(219, 97, 162, 0.15)
                    Agent->>Extract: file_url + page
                    Extract-->>Agent: full text of the page, in reading order
                end
            else count or list
                rect rgba(240, 136, 62, 0.15)
                    Agent->>Genie: question in plain English
                    Genie-->>Agent: SQL result over the register and entity list
                end
            end
            Note over Agent,Model: a rejected call goes back to the model as an error, so it can correct it
        else the model has enough
            Model-->>Agent: answer with citations, which ends the loop
        end
    end

    rect rgba(207, 34, 46, 0.12)
        opt the 8 rounds ran out
            Agent->>Model: answer now, with tools disabled
            Model-->>Agent: answer from what the tools returned
        end
    end

    rect rgba(46, 160, 67, 0.15)
        Agent->>Memory: save the question and answer, not the tool calls
    end
    Agent-->>User: answer
```

The diagram's source is also in [`diagrams/agent-sequence.mmd`](diagrams/agent-sequence.mmd), to edit in
[mermaid.live](https://mermaid.live) or render elsewhere.

| Code controls (`agent.py`) | The model decides |
|---|---|
| Loading the session's last 20 messages | Whether a question needs a tool |
| Which tools exist, and checking their arguments | Which tool, with what arguments |
| At most 8 rounds of tool calls, then one answer without tools | When it has enough to answer |
| Returning a rejected call's error to the model | How to recover from it |
| Saving the question and answer | The answer and its citations |

The model also does what `query_rewriting.py` did in lecture 2: it puts the insurer, year and class
into `search_fcr`'s filters rather than its query.

## Session memory

`memory.py` keeps each session in `<schema>.agent_session_messages_<env>` in Lakebase, next to the
synced knowledge base, with the environment in the name because every environment shares the database.
It differs from the course's version in three ways:

| | Course | Here | Why |
|---|---|---|---|
| Order | `created_at` | `id` | Messages saved together share one `created_at` (the transaction's start), so their order wasn't guaranteed |
| Connection | A pool, and the first branch listed | `lakebase.connect`, per call | A fresh OAuth token every time, and the project's `production` branch, as the sync uses |
| Errors | Logged, then an empty history | Raised | A turn without its history would answer the wrong question |

Only questions and answers are saved, not tool calls: tool results are large, and the next turn can
look facts up again.

## The `fcr_agent` job

Four independent tasks, run in parallel (`resources/fcr_agent.job.yml`):

| Task | Sets up and checks |
|---|---|
| `uc_function` | Registers `get_fcr_details`, then calls it through SQL and through its MCP server |
| `genie_space` | Asks the Genie space a question and lists its MCP tools |
| `custom_function_tools` | Runs `search_fcr` and `extract_fcr_content` against Lakebase, and one bad call |
| `session_memory` | Creates the session table; saves and loads a session, checking order |

psycopg's binary build aborts Python on serverless compute: loading its bundled libpq crashed both
Lakebase tasks with SIGABRT on environment versions 5 and 6, before any connection was made. So those two
notebooks switch psycopg to its pure-Python build, which uses the system's libpq, before importing it.
With that, all four tasks passed (run `97158545668206`).

`DatabricksMCPClient`'s synchronous methods call `asyncio.run`, which fails inside a notebook's running
event loop. `mcp.outside_event_loop` runs them on a worker thread there, so the same tools work from a
laptop, a notebook or a job without `nest_asyncio`.

The agent itself runs from a laptop (`notebooks/03_agent/fcr_agent.py`). `genie_space_id` in
`project_config.yml` (from `databricks bundle summary -t dev`) gives it the Genie tools; it's set for dev.

## Findings

- **Tools chosen well, unprompted**: asked how Wilton Re's eligible capital compared with its ECR in
  2024, the model searched with the insurer and year as filters, then read pages 15 and 16 in full and
  answered 1,757.3 against 717.1. Asked "And in 2023?", it carried the insurer and the measure over from
  the saved history, searched 2023 and read page 14 (1,547.0 against 545.2).
- **The model needs to know how FCRs report solvency**: asked how Wilton Re's *solvency* compared with
  its ECR, the model searched seven times for a stated "solvency ratio", which the FCR doesn't print,
  and ran out of rounds. Two changes fixed it: `TOOL_GUIDANCE`, added to the system prompt, says FCRs
  give eligible capital and the ECR rather than the ratio, and to read a page before searching again;
  and when the rounds run out, one last request forbids tools (`tool_choice="none"`), so the turn ends
  with what was found. The same question then took 4 tool calls and the follow-up 2.
- **Citations aren't consistent yet**: before `TOOL_GUIDANCE`, answers cited pages in a tool-style form
  (`【extract_fcr_content†page=15】`) without the URL; the latest run included the URL, but one run
  isn't enough to call it fixed.
- **Not yet verified**: answers label the figures "US$ million", which hasn't been checked against the
  PDFs; and the demo's questions don't need Genie, so the agent hasn't yet been seen choosing it.
