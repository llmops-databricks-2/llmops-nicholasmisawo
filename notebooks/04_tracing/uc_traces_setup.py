# Databricks notebook source
# MAGIC %md
# MAGIC # 4.0 Set up Unity Catalog trace storage
# MAGIC
# MAGIC The course's notebook 4.0 for this project. Run it before 4.1 and 4.2: they write traces this sets up.
# MAGIC Safe to rerun; each step checks what's there before changing anything.
# MAGIC
# MAGIC 1. **Bind the experiments to their trace tables.** Each experiment's traces live in Unity Catalog
# MAGIC    tables, `<catalog>.<schema>.<prefix>_otel_spans` and the tables and views beside it, so access is
# MAGIC    governed by UC privileges rather than workspace experiment permissions.
# MAGIC    - The agent's experiment (`experiment_path`, prefix `llmops`) was bound at onboarding: this checks it.
# MAGIC    - The tracing demos' experiment (`demo_experiment_path`, prefix `llmops_demo`, dev only) is created
# MAGIC      if missing, so 4.1's teaching traces stay out of the agent's tables. An experiment name alone
# MAGIC      isolates nothing when two experiments share tables.
# MAGIC    - An experiment already bound elsewhere stops the run: it's never silently converted.
# MAGIC 2. **Let the deployed identity write the agent's traces.** The service principal
# MAGIC    (`sp-deploy-nicholasmisawo-<env>`, its application id in the UC secret `databricks_client_id`) gets
# MAGIC    explicit `SELECT, MODIFY` on the trace tables and `SELECT` on the views. Databricks requires these
# MAGIC    explicitly: `ALL PRIVILEGES` on the schema isn't enough.
# MAGIC 3. **Check the grants took.**
# MAGIC
# MAGIC Assumed, not granted here: the principal's `USE CATALOG` / `USE SCHEMA`, provisioned with the course,
# MAGIC and `CAN USE` on the SQL warehouse for searching traces. Reading those needs admin rights.

# COMMAND ----------

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger

from fcr_curator.config import get_widget, load_config
from fcr_curator.tracing import (
    SQL_WAREHOUSE_ENV,
    bind_experiment,
    grant_trace_access,
    missing_trace_access,
    trace_grants,
    trace_location,
)

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
cfg = load_config("../../project_config.yml", env=ENV)
os.environ[SQL_WAREHOUSE_ENV] = cfg.warehouse_id  # Searching UC traces runs SQL on this warehouse
workspace = WorkspaceClient()

experiments = {cfg.experiment_name: cfg.tracing.table_prefix}
if cfg.demo_experiment_path:
    experiments[cfg.demo_experiment_path] = cfg.tracing.demo_table_prefix

# COMMAND ----------
# 1. Bind each experiment to its trace tables (create, or check the existing binding)

for experiment_name, table_prefix in experiments.items():
    experiment = bind_experiment(experiment_name, trace_location(cfg, table_prefix))
    logger.info(f"Experiment {experiment.name} (id={experiment.experiment_id})")
    logger.info(f"  trace table: {experiment.trace_location.full_otel_spans_table_name}")

# COMMAND ----------
# 2. Grant the deployed identity write access to the agent's traces

principal = dbutils.secrets.get(catalog=cfg.catalog, schema=cfg.schema, key=cfg.tracing.service_principal_secret)
tables = list(workspace.tables.list(catalog_name=cfg.catalog, schema_name=cfg.schema))
grants = trace_grants(tables, cfg.tracing.table_prefix)
if not grants:
    raise RuntimeError(f"No {cfg.tracing.table_prefix} trace tables in {cfg.full_schema_name}; run step 1 first")

grant_trace_access(workspace, grants, principal)
for grant in grants:
    logger.info(f"Granted {', '.join(privilege.value for privilege in grant.privileges)} on {grant.full_name}")

# COMMAND ----------
# 3. Check the grants took: the principal's direct grants, not ALL PRIVILEGES inherited from the schema

missing = missing_trace_access(workspace, grants, principal)
if missing:
    raise RuntimeError(f"The trace writer still lacks: {'; '.join(missing)}")
logger.info(f"The service principal can write the agent's traces in {len(grants)} trace objects")
