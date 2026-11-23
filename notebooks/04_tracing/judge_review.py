# Databricks notebook source
# MAGIC %md
# MAGIC # Check the judge against your review
# MAGIC
# MAGIC The last step of the course's 4.3 and 4.4: is the `evidence_support` judge good enough to rely on?
# MAGIC You and the judge must rate **the same answers**, so this reads the judge's verdicts from the evaluation
# MAGIC run named in `evaluation/human_labels.yml`, not from a new run, which would produce new answers.
# MAGIC
# MAGIC 1. Read `evaluation/review_sheet.md`: that run's dev answers, each with its question and evidence, and
# MAGIC    without the judge's verdicts.
# MAGIC 2. Record your verdict per case in `evaluation/human_labels.yml`.
# MAGIC 3. Run this: it compares the two, lists each disagreement with the judge's reasoning, and logs the
# MAGIC    agreement rate to that evaluation run.
# MAGIC
# MAGIC One reviewer is a calibration exercise, not evidence the judge can replace people: the notes ask for
# MAGIC several reviewers and their disagreements first, and the Alternative Annotator Test for a real claim.
# MAGIC
# MAGIC Run it locally (`uv run python notebooks/04_tracing/judge_review.py`) or as the `judge_review` task.

# COMMAND ----------

import mlflow
from loguru import logger

from fcr_curator.config import find_project_file, get_widget, load_config
from fcr_curator.evaluation import load_cases, load_human_verdicts, load_reviewed_run_id
from fcr_curator.review import judge_verdicts, run_results
from fcr_curator.scorers import judge_agreement
from fcr_curator.tracing import use_experiment

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
cfg = load_config("../../project_config.yml", env=ENV)
labels_path = find_project_file(cfg.evaluation.human_labels_file)
human = load_human_verdicts(labels_path)
RUN_ID = load_reviewed_run_id(labels_path)
experiment = use_experiment(cfg, cfg.experiment_name)

# COMMAND ----------
# Compare your verdicts with the judge's, on that run's answers

results = run_results(experiment.experiment_id, RUN_ID)
cases = {case.id for case in load_cases(find_project_file(cfg.evaluation.cases_file))}
unknown = sorted(set(human) - cases)
if unknown:
    raise ValueError(f"human_labels.yml names cases that aren't in the evaluation cases: {unknown}")

agreement = judge_agreement(judge_verdicts(results), human)
if agreement.rate is None:
    raise ValueError(f"No verdicts in {labels_path} yet: review evaluation/review_sheet.md first")
logger.info(f"Judge agreed with you on {agreement.agreed} of {agreement.compared} dev cases ({agreement.rate:.0%})")
for disagreement in agreement.disagreements:
    case = disagreement.split(":", 1)[0]
    logger.info(f"  {disagreement}\n    judge's reasoning: {results[case].rationale}")

# COMMAND ----------
# Record it on the evaluation run, with the disagreements, so it travels with the results

with mlflow.start_run(run_id=RUN_ID):
    mlflow.log_metric("judge_human_agreement", agreement.rate)
    mlflow.log_metric("judge_human_compared", agreement.compared)
    mlflow.log_text("\n".join(agreement.disagreements) or "No disagreements", "judge_review/disagreements.txt")
    mlflow.log_artifact(str(labels_path), "judge_review")
logger.info(f"Logged to evaluation run {RUN_ID}")
