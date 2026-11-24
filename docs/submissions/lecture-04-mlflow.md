# Lecture 4 submission: tracing, evaluation and a registered agent

Submitted 2026-11-23. **Project:** an assistant over the Financial Condition Reports (FCRs) that
BMA-licensed insurers and reinsurers publish. The design is in [09 Tracing](../09-tracing.md) and
[10 Evaluation](../10-evaluation.md). Everything runs in Databricks as one job, `fcr_tracing`, with a task per
course notebook (4.0 to 4.4).

| Homework item | Delivered | Where |
|---|---|---|
| 1. An `agent.py` with a clear entry point and a bounded loop | `FCRAgent`, a ResponsesAgent built from config, as the course's `ArxivAgent`; at most 8 rounds of tool calls | `src/fcr_curator/agent.py` |
| 1. Meaningful spans for the agent, tools and model calls | AGENT, MEMORY, CHAIN, LLM (with token usage), a TOOL span per tool named after it, and a RETRIEVER span for the pgvector search | [09 Tracing](../09-tracing.md#what-a-turns-trace-looks-like) |
| 1. Session and request ids, code and deployment metadata | On every trace: the session, the request id, the commit (`${bundle.git.commit}`), deployment and model version | `call_and_run_tools` |
| 1. One successful trace and one failure, with the step that caused it | Wilton Re's 76 s answer (the UC function took 47 s); AXA, whose short name the tool refused, and how the model recovered | [09 Tracing](../09-tracing.md#a-failure-and-the-step-that-caused-it) |
| 2. Cases with expected behaviour and context, including a follow-up, missing evidence and a known failure | 16 reviewed cases, 10 dev and 6 holdout, each with the FCR pages or register rows behind it | `evaluation/fcr_eval_cases.yml` |
| 2. A deterministic check and a task-specific judge | 4 code scorers; MLflow's correctness and per-case guideline judges; a custom `evidence_support` judge | `src/fcr_curator/scorers.py` |
| 2. Read outputs and rationales; a case a superficial check passes wrongly | The "3.5 times" answer states a correct figure, so `figures_stated` gives it credit; only the judges catch the wrong ratio. In version 1's run, `wilton_2024_capital_vs_ecr` scores 1.00 on figures but fails its guideline (the ratio isn't flagged as worked out) | [Evidence](../evidence/lecture-04-evaluation.md) |
| 2. Review the judge against human decisions; a holdout for changes | A blind review of version 1's 10 dev answers: **6 of 10** agreed with the judge. Of the 4 disagreements, 2 were a misread rubric and 2 real calls where the judge was right on reflection (final 10/10, no longer independent). Holdout kept apart | [10 Evaluation](../10-evaluation.md#what-the-review-found) |
| 3. Log code, configuration and metrics; register a version | `fcr_agent` version 1, from code, with both splits' metrics and the release tags; alias `latest-model` | [10 Evaluation](../10-evaluation.md#the-registered-version) |
| 3. Which identity runs it, and what it must reach | The deploy service principal, and each resource with its access and status | [10 Evaluation](../10-evaluation.md#which-identity-will-run-it-and-what-it-needs) |
| 3. Concrete version, configuration and limitations | Version 1 at commit `b584a31`, job run `461977044469071`; what still fails, below | This page |

## Tracing (4.0 to 4.2)

- **Trace storage.** Traces are kept in Unity Catalog, in `llmops_otel_*` tables bound to the agent's
  experiment at onboarding, which 4.0 checks rather than recreates.
- **The tracing demos are kept apart.** They have their own experiment and their own `llmops_demo_*` tables,
  since an experiment name isolates nothing in shared tables.
- **The service principal can now write traces.** It held `ALL PRIVILEGES` on the schema but no direct grants
  on the trace tables, which Databricks requires. 4.0 now grants `SELECT, MODIFY` and reads the grants back.
- **The agent keeps the course's span layout**, with three changes, each explained in 09 Tracing:
  - The model call has an LLM span, with token usage.
  - Memory spans are typed MEMORY, not RETRIEVER, which retrieval judges would read as evidence.
  - Retrieval has its own RETRIEVER span, with the passages recorded as documents.

## Evaluation (4.3)

- **The cases came from the data**, not from the agent's answers. Writing them:
  - confirmed Wilton Re's figures are in $ millions;
  - showed the agent's earlier "3.5 times" was wrong (it's 4.66);
  - found that "Wilton Re" is two companies.
- **The judges run on a different model family from the agent's**, and skip cases they can't score.
- **`evidence_support` gives named verdicts**, not a 1-to-5 score: `supported`, `unsupported`,
  `insufficient_evidence` or `no_claims`.

## Evidence

Per-case scores, trace ids and the judges' reasons for version 1's evaluation runs are in
[evidence/lecture-04-evaluation.md](../evidence/lecture-04-evaluation.md), exported from MLflow with workspace
URLs and user names left out. The success and failure traces are in [09 Tracing](../09-tracing.md).

## Results (4.4)

| Scorer | Baseline dev | After the fix, dev (3 runs) | Baseline holdout | After the fix, holdout (3 runs) |
|---|---|---|---|---|
| `answered` | 0.80 | 1.00, 0.90, 0.90 | 0.83 | 1.00, 0.83, 1.00 |
| `correctness` | 0.67 | 0.67, 0.56, 0.67 | 0.60 | 0.60, 0.60, 0.60 |
| `cites_page_urls` | 0.40 | 0.20, 0.40, 0.20 | 0.50 | 0.50, 0.50, 0.50 |
| `no_tool_markup` | 0.50 | 0.40, 0.50, 0.50 | 0.50 | 0.67, 0.50, 0.50 |

## What changed after inspection

- **The harness, first.** The first baseline measured my evaluation code more than the agent. Reading cases
  and traces found:
  - MLflow sometimes scored the agent's raw response instead of its answer;
  - figures with narrow spaces weren't read;
  - the judge counted true links as unsupported;
  - one case was wrong: Somerset Re reports 2023 on two bases.

  All were fixed before comparing anything.
- **One targeted change to the agent** (`f5fe97d`). The model sometimes ended a turn with an empty reply,
  which became the answer, in 3 of 16 cases. Now it's asked again without tools. No case was empty after the
  change.

## What still fails, and what's next

- **Citations.** About half the answers copy tool-style markers (【…】), and most don't link the page. This is
  the next change to test.
- **"Not filed" for "not found".** Some answers say an insurer has no published FCR, where the register only
  knows none was found.
- **A wrong figure in one run.** Argo Re's ratio was given as 100% for both years, against 142% and 123%.
- **Running out of rounds** on questions with no answer, rather than saying so sooner.
- **Run-to-run variation.** The means moved by up to 0.5 between runs of the same code. One run per version
  can't show a change helped beyond the failure it targeted, so comparisons need several runs, or more cases.
- **The judge isn't validated to replace review.** One reviewer agreed with it on 6 of 10 answers blind.
  Two of the four disagreements came from the rubric being misread, so the next review starts from worked
  examples. Replacing review would need several reviewers, their disagreements first, and the Alternative
  Annotator Test.
