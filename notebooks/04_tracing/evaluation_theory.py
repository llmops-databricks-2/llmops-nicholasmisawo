# Databricks notebook source
# MAGIC %md
# MAGIC # 4.3 Evaluating the FCR agent's answers
# MAGIC
# MAGIC The course's notebook 4.3 for this project. Tracing (4.1, 4.2) shows how an answer was produced;
# MAGIC evaluation asks whether it's right. A turn can finish without an error and still state a wrong figure.
# MAGIC
# MAGIC Each scorer here is shown on **fixed answers** (a good one, a wrong one, a refusal), so you can see what
# MAGIC it catches without the agent's variability; 4.4 runs them on the agent itself. Runs go to the **demo
# MAGIC experiment**, apart from the agent's.
# MAGIC
# MAGIC | Section | Shows |
# MAGIC |---|---|
# MAGIC | 1 | The evaluation cases, and why each split exists |
# MAGIC | 2 | Code scorers: cheap and exact, but shallow |
# MAGIC | 3 | Guidelines: plain-language rules, pass or fail |
# MAGIC | 4 | Built-in judges: correctness and per-case guidelines |
# MAGIC | 5 | A custom categorical judge: are the claims backed by the evidence? |
# MAGIC | 6 | All scorers together |
# MAGIC | 7 | Checking the judge against a person's verdicts |
# MAGIC | 8 | Do's and don'ts |

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build (docs/08-agent.md): the scorers import the agent's constants

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

from collections import Counter

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import display
from loguru import logger
from mlflow.genai.scorers import Guidelines

from fcr_curator.config import find_project_file, get_widget, load_config
from fcr_curator.evaluation import load_cases
from fcr_curator.scorers import (
    answered,
    cites_page_urls,
    evidence_support_judge,
    fcr_scorers,
    figures_stated,
    judge_agreement,
    judge_uri,
    no_tool_markup,
)
from fcr_curator.tracing import use_experiment, workspace_path

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
cfg = load_config("../../project_config.yml", env=ENV)
if not cfg.demo_experiment_path:
    raise ValueError(f"The evaluation demos run in dev; {ENV} has no demo_experiment_path in project_config.yml")
workspace = WorkspaceClient()
experiment = use_experiment(cfg, workspace_path(cfg.demo_experiment_path, workspace.current_user.me().user_name))
JUDGE = judge_uri(cfg.evaluation.judge_model)
logger.info(f"Evaluating in {experiment.name}; judges run on {cfg.evaluation.judge_model}, not the agent's model")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. The evaluation cases
# MAGIC
# MAGIC `evaluation/fcr_eval_cases.yml` holds 16 reviewed cases: a question, the facts a good answer states
# MAGIC and the guidelines it follows, and the FCR pages or register rows behind them. The **dev** split is for
# MAGIC finding and fixing failures; the **holdout** is kept back to judge whether a change helped, so the
# MAGIC agent isn't tuned to the questions that grade it. Both cover the same kinds of question.

# COMMAND ----------

cases = load_cases(find_project_file(cfg.evaluation.cases_file))
display(
    [
        {"split": split, "category": category, "cases": count}
        for (split, category), count in sorted(Counter((case.split, case.category) for case in cases).items())
    ]
)
by_id = {case.id: case for case in cases}

# COMMAND ----------

# MAGIC %md
# MAGIC Three fixed answers to the case `wilton_capital_vs_msm`, plus a refusal for `investment_advice`. The
# MAGIC wrong answer is the agent's own from lecture 3: "roughly 3.5 times", with no MSM in its evidence.

# COMMAND ----------

GOOD = (
    "Wilton Re's minimum margin of solvency (MSM) at 31 December 2024 was 377.0 ($ millions) "
    "(https://www.wiltonre.bm/wp-content/uploads/2025/04/WREB-Financial-Condition-Report-2024.pdf#page=16). "
    "Total eligible capital was 1,757.3 (page 15), so about 4.66 times the MSM; I worked that ratio out."
)
WRONG = "Wilton Re's eligible capital of 1,757.3 was roughly 3.5 times its minimum regulatory requirement."
TOOL_MARKUP = "Eligible capital was 1,757.3 against an MSM of 377.0, about 4.66 times 【extract_fcr_content†page=16】."
REFUSAL = "I can't give investment advice. I can tell you what Arch's FCRs report about its capital, if that helps."


def scored(case_id: str, output: str) -> dict:
    """Return the case as an evaluation record with a fixed answer."""
    return {**by_id[case_id].record(), "outputs": output}


examples = [
    scored("wilton_capital_vs_msm", GOOD),
    scored("wilton_capital_vs_msm", WRONG),
    scored("wilton_capital_vs_msm", TOOL_MARKUP),
    scored("investment_advice", REFUSAL),
]
LABELS = ("good", "wrong ratio", "tool markup", "refusal")


def show(results: mlflow.models.EvaluationResult) -> None:
    """Display each example's scores, labelled, with the scorers' rationales where they give one."""
    table = results.result_df
    columns = [column for column in table.columns if column.endswith(("/value", "/rationale"))]
    columns = [column for column in columns if not column.startswith(("expected_facts", "guidelines", "evidence"))]
    display(table[columns].assign(example=LABELS[: len(table)])[["example", *columns]])


# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Code scorers
# MAGIC
# MAGIC Python functions over the answer and the case's expectations. Exact, free and repeatable, but shallow:
# MAGIC stating a figure isn't stating it correctly. A scorer that doesn't apply to a case returns nothing, so
# MAGIC the case is left out of its mean instead of passing or failing it.
# MAGIC
# MAGIC | Scorer | Checks |
# MAGIC |---|---|
# MAGIC | `answered` | The turn ended with an answer, not the no-answer fallback |
# MAGIC | `figures_stated` | The share of the expected figures stated (245% counts as 2.45) |
# MAGIC | `cites_page_urls` | An FCR page is linked (skipped when the evidence is the register) |
# MAGIC | `no_tool_markup` | No tool-style citations copied from tool output |

# COMMAND ----------

show(mlflow.genai.evaluate(data=examples, scorers=[answered, figures_stated, cites_page_urls, no_tool_markup]))

# COMMAND ----------

# MAGIC %md
# MAGIC The wrong answer still states 1,757.3, so `figures_stated` gives it partial credit: a code check
# MAGIC can't tell "3.5 times" is wrong. That needs a judge.
# MAGIC
# MAGIC ## 3. Guidelines: plain-language rules, pass or fail

# COMMAND ----------

found_not_filed = Guidelines(
    name="found_not_filed",
    guidelines=[
        "When the answer says an insurer has no FCR, it must say none was found, not that the insurer "
        "hasn't filed or published one: the register only knows what the pipeline found."
    ],
    model=JUDGE,
)
register_examples = [
    {**by_id["insurers_without_fcr_by_class"].record(), "outputs": answer}
    for answer in (
        "No FCR was found for 70 Long Term Class E, 36 Class 4, 29 Class 3B and 14 group insurers.",
        "70 Long Term Class E, 36 Class 4, 29 Class 3B and 14 group insurers haven't filed an FCR yet.",
    )
]
show(mlflow.genai.evaluate(data=register_examples, scorers=[found_not_filed]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Built-in judges
# MAGIC
# MAGIC `Correctness` checks each expected fact is in the answer; `ExpectationsGuidelines` checks the case's
# MAGIC own guidelines. Both fail a case that lacks what they need (a refusal has no expected facts), so
# MAGIC `fcr_scorers` wraps them to skip such cases. Here they're on the Wilton examples, which have both.

# COMMAND ----------

builtin = [
    scorer
    for scorer in fcr_scorers(cfg.evaluation.judge_model)
    if scorer.name in ("correctness", "expectations_guidelines")
]
show(mlflow.genai.evaluate(data=examples[:3], scorers=builtin))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. A custom categorical judge
# MAGIC
# MAGIC `evidence_support` reads the answer against the case's evidence (the quoted FCR pages and register
# MAGIC rows) and returns one of four verdicts, the lecture notes' rubric with one addition:
# MAGIC
# MAGIC | Verdict | When |
# MAGIC |---|---|
# MAGIC | `supported` | Every factual claim is backed by the evidence |
# MAGIC | `unsupported` | A claim contradicts the evidence, or goes beyond it |
# MAGIC | `insufficient_evidence` | The answer says the information isn't there, and the evidence agrees |
# MAGIC | `no_claims` | No factual claim at all (a refusal), so there's nothing to support |
# MAGIC
# MAGIC Categories rather than the course's 1-to-5 quality score: an integer doesn't make the judge apply a
# MAGIC rubric consistently, and "how good, out of 5" mixes support, completeness and tone. A named verdict
# MAGIC says what was wrong, and leaves completeness to `correctness`.

# COMMAND ----------

show(mlflow.genai.evaluate(data=examples, scorers=[evidence_support_judge(JUDGE)]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. All the scorers together
# MAGIC
# MAGIC The set 4.4 runs on the agent. Each catches what the others miss: the code checks citations and
# MAGIC figures exactly, `correctness` completeness, `expectations_guidelines` the case's rules,
# MAGIC `evidence_support` whether the claims are true to the source.

# COMMAND ----------

all_results = mlflow.genai.evaluate(data=examples, scorers=fcr_scorers(cfg.evaluation.judge_model))
show(all_results)
logger.info(f"Means: {all_results.metrics}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Checking the judge against a person
# MAGIC
# MAGIC A judge is another model, and can be wrong. Before trusting `evidence_support`, compare its verdicts
# MAGIC with a person's on the same answers, read with the same evidence. Here the "person" is the label each
# MAGIC example was written to have; 4.4 compares against your real verdicts in `evaluation/human_labels.yml`.
# MAGIC
# MAGIC Agreement on a handful of answers can reveal a rubric problem, but it isn't enough to replace human
# MAGIC review (the notes' Alternative Annotator Test describes what would be). MLflow can also align a judge's
# MAGIC instructions to human feedback (`judge.align(...)`, with the SIMBA optimizer); that needs real reviewed
# MAGIC traces, and examples kept apart from those used to test the judge.

# COMMAND ----------

written_to_be = {"good": "supported", "wrong ratio": "unsupported", "tool markup": "supported", "refusal": "no_claims"}
judge_said = dict(zip(LABELS, all_results.result_df["evidence_support/value"], strict=True))
agreement = judge_agreement(judge_said, written_to_be)
logger.info(
    f"Judge agreed on {agreement.agreed} of {agreement.compared}: {agreement.disagreements or 'no disagreements'}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Do's and don'ts
# MAGIC
# MAGIC **Do**
# MAGIC 1. Write cases from the task, with the evidence behind each expected fact, before choosing metrics.
# MAGIC 2. Keep a holdout, and judge changes on it rather than on the cases you tuned with.
# MAGIC 3. Combine code checks (exact, cheap) with judges (meaning), each for the failure it catches.
# MAGIC 4. Run the judge on a different model family from the agent's.
# MAGIC 5. Read each case's answer and the judge's rationale before trusting a mean.
# MAGIC 6. Check the judge against people's verdicts, and recheck when the judge, rubric or model changes.
# MAGIC 7. Add a case for every failure you find (as `wilton_capital_vs_msm` and `axa_short_name` were).
# MAGIC
# MAGIC **Don't**
# MAGIC 1. Trust one metric, or a format check (length, a keyword) as evidence of correctness.
# MAGIC 2. Let a judge grade its own model's answers.
# MAGIC 3. Score a case on a rule that doesn't apply to it: skip it.
# MAGIC 4. Use an integer score as if it enforced a rubric.
# MAGIC 5. Treat an evaluation result as a release gate unless code acts on it.
