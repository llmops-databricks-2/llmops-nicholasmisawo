# Databricks notebook source
# MAGIC %md
# MAGIC # 4.4 Evaluate the FCR agent, then log and register it
# MAGIC
# MAGIC The course's notebook 4.4 for this project. Run 4.0 first: it grants the trace access this writes with.
# MAGIC
# MAGIC 1. **Evaluate** the agent on the dev and holdout cases with every scorer from 4.3. Each case is a traced
# MAGIC    request in the agent's experiment, in a session of its own; a follow-up's earlier question is asked
# MAGIC    in that session first, so its history comes from memory as in a real conversation.
# MAGIC 2. **Name the dev run to review**: checking the judge against you happens in `judge_review.py`, on that
# MAGIC    run's answers.
# MAGIC 3. **Log** the agent (models from code: `agent_model.py`) with both splits' metrics, **register** it as a
# MAGIC    version of `<catalog>.<schema>.fcr_agent`, and point the `latest-model` alias at it.
# MAGIC
# MAGIC Set the `register` widget (job parameter) to `false` to evaluate without registering, when comparing
# MAGIC a change. Registering deploys nothing, and isn't a release gate: read the metrics before using a version.

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build with the system's libpq: its binary build aborted Python
# on serverless compute (docs/08-agent.md). Set before anything imports psycopg.

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

import math
from uuid import uuid4

import mlflow
from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import display
from loguru import logger

from fcr_curator.agent import FCRAgent
from fcr_curator.config import find_project_file, get_widget, load_config
from fcr_curator.evaluation import SPLITS, load_cases, make_predict_fn, split_records
from fcr_curator.mcp import DEV_GENIE_SPACE_ID
from fcr_curator.registration import Release, log_and_register, prefixed_metrics
from fcr_curator.scorers import fcr_scorers
from fcr_curator.tracing import use_experiment

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
GENIE_SPACE_ID = get_widget("genie_space_id", DEV_GENIE_SPACE_ID)
GIT_SHA = get_widget("git_sha", "local")
JOB_RUN_ID = get_widget("job_run_id", "local")
REGISTER = get_widget("register", "true").lower() == "true"
os.environ["GIT_SHA"] = GIT_SHA  # The agent tags each trace with it

cfg = load_config("../../project_config.yml", env=ENV)
# Cases run in parallel threads; a few at once keep the agent within the gateway's 60 requests a minute
os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(cfg.evaluation.max_workers)
experiment = use_experiment(cfg, cfg.experiment_name)
workspace = WorkspaceClient()
agent = FCRAgent.from_config(cfg, ENV, GENIE_SPACE_ID, workspace)
cases = load_cases(find_project_file(cfg.evaluation.cases_file))
logger.info(f"Evaluating {len(cases)} cases in {experiment.name}, judged by {cfg.evaluation.judge_model}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Evaluate on both splits
# MAGIC
# MAGIC One evaluation run per split, so each has its own results in the experiment's **Evaluations** tab,
# MAGIC every case linked to its trace.

# COMMAND ----------

predict_fn = make_predict_fn(agent)
scorers = fcr_scorers(cfg.evaluation.judge_model)
cases_path = find_project_file(cfg.evaluation.cases_file)
# What produced these scores, so a later run can be compared like for like: agent, judge, prompt and cases
evaluation_config = {
    "agent_model": cfg.llm_endpoint,
    "judge_model": cfg.evaluation.judge_model,
    "max_tool_rounds": cfg.agent.max_tool_rounds,
    "cases_file": cfg.evaluation.cases_file,
}
results, run_ids = {}, {}
for split in SPLITS:
    with mlflow.start_run(
        run_name=f"fcr-eval-{split}-{GIT_SHA[:7]}", tags={**Release(GIT_SHA, JOB_RUN_ID).tags(), "split": split}
    ) as run:
        mlflow.log_params({**evaluation_config, "cases": len(split_records(cases, split))})
        mlflow.log_artifact(str(cases_path), "evaluation")
        mlflow.log_text(cfg.system_prompt, "evaluation/system_prompt.txt")
        results[split] = mlflow.genai.evaluate(data=split_records(cases, split), predict_fn=predict_fn, scorers=scorers)
        run_ids[split] = run.info.run_id
    logger.info(f"{split}: {results[split].metrics}")
dev_run_id = run_ids["dev"]

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Read each case, not just the means
# MAGIC
# MAGIC A mean hides which case failed and why. Each row is one case: its scores, and the judge's reasons.

# COMMAND ----------

SCORES = (
    "answered",
    "figures_stated",
    "cites_page_urls",
    "no_tool_markup",
    "correctness",
    "expectations_guidelines",
    "evidence_support",
)


def score_cell(value: object) -> str:
    """Return a score as text: one column type for display, and blank where a scorer skipped the case."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


def case_rows(split: str) -> list[dict]:
    """Return one row per case: its id, category and each scorer's value."""
    table = results[split].result_df
    return [
        {"case": row["tags"].get("case_id"), "category": row["tags"].get("category")}
        | {name: score_cell(row.get(f"{name}/value")) for name in SCORES}
        for _, row in table.iterrows()
    ]


for split in SPLITS:
    display(case_rows(split))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Checking the judge
# MAGIC
# MAGIC Your verdicts must be on the same answers as the judge's, and this run has just produced new ones. So
# MAGIC the check is separate: set `run_id` in `evaluation/human_labels.yml` to a dev run printed below, review
# MAGIC its answers (`evaluation/review_sheet.md`), then run `judge_review.py`, which logs the agreement to
# MAGIC that run.

# COMMAND ----------

logger.info(f"Dev evaluation run, for review: {dev_run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Log and register

# COMMAND ----------

metrics = prefixed_metrics("dev", results["dev"].metrics) | prefixed_metrics("holdout", results["holdout"].metrics)

if not REGISTER:
    logger.info("register=false: evaluated only")
else:
    input_example = {
        "input": [{"role": "user", "content": "What was Wilton Re's enhanced capital requirement in 2024?"}],
        "custom_inputs": {"session_id": f"log-model-{uuid4()}", "request_id": f"req-{uuid4()}"},
    }
    version = log_and_register(cfg, ENV, GENIE_SPACE_ID, Release(GIT_SHA, JOB_RUN_ID), metrics, input_example)
    logger.info(f"Registered {cfg.registered_model_name} version {version.version} as {cfg.evaluation.model_alias}")
