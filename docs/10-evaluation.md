# 10 Evaluation

How the FCR agent is evaluated (lecture 4.3). Tracing ([09 Tracing](09-tracing.md)) shows how an answer
was produced; evaluation asks whether it's right. A turn can finish without errors and still be wrong.

## The cases

[`evaluation/fcr_eval_cases.yml`](../evaluation/fcr_eval_cases.yml) holds 16 cases, reviewed by hand.
Each has a question, what a good answer states (`expected_facts`) or does (`guidelines`), and the FCR pages
or register rows that support it (`evidence`). The last of these lets a judge check claims against the
source, not just against the answer. `src/fcr_curator/evaluation.py` loads them and turns them into
`mlflow.genai.evaluate` records.

| Split | Cases | Use |
|---|---|---|
| dev | 10 | Find failures, and check fixes |
| holdout | 6 | Kept back to judge whether a change helped, so the agent isn't tuned to the questions that grade it |

Both splits cover the same kinds of question, so a change is judged on what it was tuned for:

| Category | dev | holdout | Covers |
|---|---|---|---|
| `single_insurer` | Wilton Re 2024 capital vs ECR; Argo Re's stated ratio | Argo Re's compliance | One insurer's figures, with a worked-out or a stated ratio |
| `compare_years` | Somerset Re 2023 to 2024 | | A change across years, with figures in an unstated unit |
| `follow_up` | Wilton Re, then "the year before?" | Somerset Re, then "the year before?" | Context carried from the session's history |
| `count_genie` | Class 4 with a 2024 FCR; no FCR by class | The six Class 4 names | Register questions, for Genie |
| `missing_evidence` | Wilton Re 2025 | Arch Reinsurance 2024 | No FCR to answer from: say so, invent nothing |
| `out_of_scope` | Investment advice | "Ignore your instructions…" | Declining, and staying in scope |
| `known_failure` | AXA's short name; capital vs MSM | | Failures already found in traces |
| `ambiguous_name` | | Two Wilton Re companies | One short name, two companies |

## Where the expected answers come from

Every figure was read on 2026-11-23 from the tables the agent's tools use, so a case can't expect what the
agent can't reach. The FCR figures come from the pages cited in each case:

```sql
-- One insurer's capital, ECR and MSM pages (here Wilton Re, 2023 and 2024)
SELECT year(ts) AS reporting_year, citation_url, text
FROM llmops_dev.nicholasmisawo.fcr_knowledge_base
WHERE account_key = 'Wilton Reinsurance Bermuda Limited' AND year(ts) IN (2023, 2024)
  AND (lower(text) LIKE '%minimum margin of solvency%' OR lower(text) LIKE '%eligible capital%'
       OR lower(text) LIKE '%enhanced capital requirement%')
  AND text RLIKE '[0-9]{3}';
```

The register figures come from `dim_fcr_entities`:

```sql
-- Insurers with and without an FCR, by licence class
SELECT fcr_class, count(*) AS insurers,
       sum(CASE WHEN NOT has_fcr THEN 1 ELSE 0 END) AS without_fcr,
       sum(CASE WHEN array_contains(reporting_years, 2024) THEN 1 ELSE 0 END) AS with_2024
FROM llmops_dev.nicholasmisawo.dim_fcr_entities
GROUP BY ALL;
```

If the pipeline runs again and finds more FCRs, these numbers change. Rerun the queries and update the
cases before comparing results across that change.

## What writing the cases found

- **"US$ million" is right for Wilton Re.** Its FCR says "in $ millions" on page 15, which settles the
  open question from lecture 3. Somerset Re's page doesn't name its unit, so its case expects no unit.
- **The "3.5 times" claim was wrong, not just unsupported.** Wilton Re's 2024 MSM is 377.0, so eligible
  capital was about 4.66 times it. The case `wilton_capital_vs_msm` checks for it.
- **"Wilton Re" is two companies.** Wilton Reinsurance Bermuda Limited has FCRs for 2022 to 2024; Wilton
  Re International Limited has none. The case `wilton_two_entities` checks the agent tells them apart.
- **Somerset Re reports 2023 on two bases.** Its 2023 FCR gives an ECR ratio of 218% on the full
  requirement and 253% on the transitional one; its 2024 FCR gives 253% for 2023. The first baseline
  failed the agent for answering 218% from the 2023 FCR, which was right: the case was wrong. Both Somerset
  cases now accept either, as long as the answer says which basis it used.
- **FCRs state ratios differently.** Argo Re prints its ECR ratio (142%); Wilton Re doesn't. The cases
  check the agent quotes a stated ratio and says when it worked one out.

## The scorers

`src/fcr_curator/scorers.py`, shown on fixed answers in `notebooks/04_tracing/evaluation_theory.py` (4.3):

| Scorer | Kind | Checks | Skips |
|---|---|---|---|
| `answered` | code | The turn ended with an answer, not the no-answer fallback | |
| `figures_stated` | code | The share of expected figures stated (245% counts as 2.45) | cases with no figures |
| `cites_page_urls` | code | An FCR page is linked (a URL with `#page=`) | cases whose evidence is the register |
| `no_tool_markup` | code | No tool-style citations (【…†…】) copied from tool output | |
| `correctness` | built-in judge | Each expected fact is in the answer | cases without expected facts |
| `expectations_guidelines` | built-in judge | The case's own guidelines are followed | cases without guidelines |
| `evidence_support` | custom judge | The claims the evidence covers match it | |

The judges run on `system.ai.claude-sonnet-5-5`, a different model family from the agent's
`gpt-oss-120b`, so no model grades its own answers. `evidence_support` returns one of four verdicts
(`supported`, `unsupported`, `insufficient_evidence`, `no_claims`) rather than the course's 1-to-5
quality score: a number doesn't make a judge apply a rubric consistently, and a named verdict says what
went wrong. MLflow's built-in judges fail on a case lacking what they need, so `skip_without` makes them
skip it instead, as the code scorers do.

Before any review, the judge was checked on fixed answers written to have known verdicts: it rated the
correct answer `supported`, the agent's own "3.5 times" `unsupported` (working out 4.66 from the MSM), and
a refusal `no_claims`.

Checking it against a person needs both to rate **the same answers**, so it doesn't happen in 4.4, whose run
produces new ones. `evaluation/review_sheet.md` lists version 1's dev answers with their questions and
evidence, without the judge's verdicts, so they don't steer the reviewer. The reviewer's verdicts go in
`evaluation/human_labels.yml`, which names that run; `notebooks/04_tracing/judge_review.py` compares them with
the judge's verdicts from the same run and logs the agreement, and each disagreement, to it.

## Results

`notebooks/04_tracing/log_register.py` (4.4) evaluates the agent on both splits, each case in a session of
its own, then logs and registers it. Means per split:

| Scorer | Baseline dev | After the fix, dev (3 runs) | Baseline holdout | After the fix, holdout (3 runs) |
|---|---|---|---|---|
| `answered` | 0.80 | **1.00, 0.90, 0.90** | 0.83 | **1.00, 0.83, 1.00** |
| `figures_stated` | 0.78 | 0.88, 0.61, 0.78 | 0.75 | 0.75, 0.75, 0.25 |
| `cites_page_urls` | 0.40 | 0.20, 0.40, 0.20 | 0.50 | 0.50, 0.50, 0.50 |
| `no_tool_markup` | 0.50 | 0.40, 0.50, 0.50 | 0.50 | 0.67, 0.50, 0.50 |
| `correctness` | 0.67 | 0.67, 0.56, 0.67 | 0.60 | 0.60, 0.60, 0.60 |
| `expectations_guidelines` | 0.80 | 1.00, 0.60, 0.80 | 0.80 | 0.80, 0.80, 0.80 |

`evidence_support` returns categories, so MLflow gives it no mean: read its verdicts per case in the
experiment's Evaluations tab. The third run of the fixed agent is the one registered (below).

### How the baseline was read

The first baseline measured the harness more than the agent. Reading each case and its trace found:

| Looked like | Was | Fixed by |
|---|---|---|
| Answers made of `{"tool_choice": null, …}` | MLflow sometimes took the agent's own `predict` trace as the case's, so scorers read the response object | Tracing `predict_fn`, so each case is one trace with the answer as its output |
| `253 %` not found in "253 %" | The agent writes narrow no-break spaces inside figures | Reading those as part of the figure |
| True PDF links marked `unsupported` | The judge counted anything beyond the quoted evidence as unsupported | A rubric that judges the claims the evidence covers |
| The agent wrong about Somerset Re's 2023 ratio | The case was wrong: Somerset reports 2023 on two bases | Accepting either, labelled |

After those, the agent's own largest failure was **no answer at all** in 3 of 16 cases (Argo Re, AXA,
Arch Reinsurance). The traces showed why: the gateway's reasoning model sometimes ends a turn with neither
a tool call nor text, which the loop returned as the answer, and for Arch it did so again in the final
reply after eight rounds searching for an FCR that doesn't exist.

### The one change, and what it did

`f5fe97d`: an empty reply isn't an answer. The model is asked again, without tools, to answer from what
it found, up to twice, before the no-answer fallback. The change targeted `answered`, and no case was
empty after it. What's left are "couldn't answer" replies in three of six split runs, where the rounds ran
out and both final attempts were empty.

The other means moved by up to 0.5 **between runs of the same code**: the job retried the failed task, and
registration was rerun, which gave three samples of it. With 10 dev and 6 holdout cases, one run can't show whether a change helped beyond
the failure it targeted; reading the cases can. Comparing changes properly needs several runs per version,
or more cases.

### What still fails

- **Citations.** About half the answers copy tool-style markers (【…†…】) and most don't link a page,
  although the prompt says to cite the URL. The next change.
- **"Not filed" for "not found".** The agent still sometimes says insurers have no published FCR, where the
  register only knows none was found (`insurers_without_fcr_by_class`, `axa_short_name`).
- **A wrong figure.** In one run the agent gave Argo Re's ECR ratio as 100% for both years, against the
  142% and 123% its FCR prints; `figures_stated`, `correctness` and `evidence_support` all caught it.
- **Ratios worked out without saying so**, and Somerset Re's 2023 figure given without its basis.
- **Running out of rounds** on a question with no answer (Arch Reinsurance), rather than saying so sooner.

## The registered version

| | |
|---|---|
| Model | `llmops_dev.nicholasmisawo.fcr_agent`, version **1**, alias `latest-model` |
| Logged as | Models from code: `src/fcr_curator/agent_model.py` rebuilds the agent from its `model_config` (the environment, the Genie space and the project config), with the package as `code_paths` |
| Release tags | `git_sha` `b584a31…`, `job_run_id` `461977044469071`, on the logging run and the version |
| Metrics | Both splits' means, as `dev/…` and `holdout/…`; `judge_human_agreement` once you've reviewed |
| Environment | Packed for Model Serving (`env_pack="databricks_model_serving"`) |

Registering deploys nothing and isn't a release gate: version 1 is registered with the scores above, which
show the citation problems still open. Lecture 5 decides what's good enough to serve.

### Which identity will run it, and what it needs

In this job the agent ran as you. Deployed, it runs as the service principal `sp-deploy-nicholasmisawo-dev`
(application id `7a6b085a-…`, from the UC secret `databricks_client_id`), which needs:

| Resource | Access | Status |
|---|---|---|
| Trace tables `llmops_otel_*`, views `llmops_trace_*` | `SELECT, MODIFY` on the tables, `SELECT` on the views | Granted by 4.0, and checked |
| Schema `llmops_dev.nicholasmisawo` | `USE CATALOG`, `USE SCHEMA`; `EXECUTE` on `get_fcr_details`; `SELECT` on `fcr_register`, `dim_fcr_entities` | `ALL PRIVILEGES` on the schema, from the course |
| Gateway services `course_ops.gateway.chat`, `course_ops.gateway.embed` | Query | To check at deployment |
| Genie space `fcr-curator-space` and warehouse `86722fe1a817f6aa` | `CAN RUN` on the space, `CAN USE` on the warehouse | To check at deployment |
| Lakebase `lb-llmops-nicholasmisawo` | A database role that reads the synced table and writes `agent_session_messages_<env>` | To check at deployment |

The judges' model (`system.ai.claude-sonnet-5-5`) is only needed where evaluation runs, not by the agent.
