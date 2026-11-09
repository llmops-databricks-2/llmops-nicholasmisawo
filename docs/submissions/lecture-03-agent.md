# Lecture 3 submission: an agent with tools and memory

Submitted 2026-11-09. **Project:** an assistant over the Financial Condition Reports (FCRs) that
BMA-licensed insurers and reinsurers publish. The design is described in [08 Agent](../08-agent.md),
which also draws one turn as a sequence diagram.

| Assignment step | Delivered |
|---|---|
| 1. Identify tools for the use case | `search_fcr`, `get_fcr_details`, `extract_fcr_content`, plus Genie |
| 2. Sketch the orchestration | A Mermaid sequence diagram, and a table of what code controls and what the model decides |
| 3. Connect tools through custom functions or MCP, including Genie | Custom functions, a UC function through managed MCP, a Genie space through managed MCP |
| 4. Save conversation history in Lakebase, load it on the next turn | `memory.py`: one row per message, the last 20 loaded each turn |
| 5. Submit the implementation and design choices through a PR | This page and [08 Agent](../08-agent.md) |

## 1. Tools

The FCR tools (`search_fcr`, `get_fcr_details`, `extract_fcr_content`) map onto each technique the
course covers, where it fits:

| Tool | Technique | Answers |
|---|---|---|
| `search_fcr` | Lakebase pgvector, hybrid search | Which passages discuss a topic, filtered by insurer, year or licence class |
| `get_fcr_details` | Unity Catalog function, through managed MCP | Which FCRs an insurer has: years, class, business, PDF links |
| `extract_fcr_content` | Custom Python function | The full text of one cited page, to quote its figures |
| Genie | Genie space, through managed MCP | Counts and lists across insurers |

Following the course's tool design practices, each tool has a description saying when to use it,
type hints, validated inputs (a blank query, an unknown class, an implausible year, more than 10
results or a name too short to match one insurer are refused with a message saying what to change),
JSON results capped in size, and errors the model can act on.

## 2. Orchestration

| Code controls (`agent.py`) | The model decides |
|---|---|
| Loading the session's last 20 messages | Whether a question needs a tool |
| Which tools exist, and checking their arguments | Which tool, with what arguments |
| At most 8 rounds of tool calls, then one answer without tools | When it has enough to answer |
| Returning a rejected call's error to the model | How to recover from it |
| Saving the question and answer | The answer and its citations |

## 3. Connecting the tools

- **Custom functions**: `search_fcr` and `extract_fcr_content` are Python functions registered with the
  agent as `ToolInfo`s, reading the Lakebase copy of the knowledge base.
- **UC function**: `llmops_dev.nicholasmisawo.get_fcr_details` is SQL over `fcr_register` and
  `dim_fcr_entities`, registered by the `fcr_agent` job and reached through
  `/api/2.0/mcp/functions/llmops_dev/nicholasmisawo/get_fcr_details`.
- **Genie**: `resources/genie_space.yml` deploys a space over the same two tables, with column
  descriptions, instructions and example SQL; the agent reaches it through `/api/2.0/mcp/genie/<id>`.

## 4. Session memory

`<schema>.agent_session_messages_<env>` in Lakebase holds one row per message. Each turn loads the
session's last 20 messages and saves the question and answer, not the tool calls. Compared with the
course's version, messages are ordered by `id` (those saved together share a `created_at`), each call
gets a fresh OAuth token, and failures are raised rather than logged and skipped.

## Results

| Check | Result |
|---|---|
| Unit tests | 232 passing, the agent's loop, tools and memory tested with fakes |
| `fcr_agent` job | All four tasks passed in parallel (run `97158545668206`): UC function by SQL and MCP, Genie question and MCP tools, both Lakebase tools, and a session saved and loaded in order |
| *How did Wilton Re's solvency compare with its ECR in 2024?* | 4 tool calls: details, search, pages 15 and 16. Eligible capital 1,757.3 against an ECR of 717.1, about 2.45 times, which the model worked out and said so |
| *And how did that compare with the year before?* | 2 tool calls, the insurer and measure taken from the history: 1,547.0 against 545.2 in 2023, about 2.84 times |

Both questions ran through `notebooks/03_agent/fcr_agent.py` with all five tools and Lakebase memory: the
session (`demo-8032869e-…`) holds both exchanges, and the follow-up was answered from it.

Genie was tested through the agent too, in a separate session:

| Genie question | Result |
|---|---|
| *How many listed insurers have no FCR yet, by licence class?* | Genie chosen unprompted: 70 Long Term Class E, 36 Class 4, 29 Class 3B, 14 Groups, matching direct SQL |
| *And how many Class 4 insurers do have an FCR for 2024?* | 6, matching direct SQL, in one plain-English Genie call (7 calls before the model was told not to send Genie SQL) |

## What the build found

- **Two platform constraints**: psycopg's binary build aborts Python on serverless compute, so the job's
  Lakebase notebooks use its pure-Python build; and the MCP client can't run inside a notebook's event
  loop, so its calls run on a worker thread there.
- **The model needed domain guidance**: asked about *solvency*, it searched repeatedly for a ratio the FCR
  doesn't print, until it ran out of rounds. Telling it that FCRs give eligible capital and the ECR, and
  ending every turn with an answer, fixed it.
- **Genie needs plain English**: the model first sent Genie SQL against tables it invented; told to ask
  in plain English, it needed one call instead of seven.
- **Still open**: answers don't always include the citation URL (the latest run did, earlier ones
  didn't); "US$ million" hasn't been checked against the PDFs; and the agent says insurers haven't
  *filed* an FCR when the register only knows none was *found*.
