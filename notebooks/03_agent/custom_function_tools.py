# Databricks notebook source
# MAGIC %md
# MAGIC # Check the Lakebase tools: search_fcr and extract_fcr_content
# MAGIC
# MAGIC Runs the two tools the agent calls in-process (`agent_tools.py`), the way the agent would:
# MAGIC
# MAGIC 1. **search_fcr** (Lakebase pgvector): hybrid search for Wilton Re's 2024 solvency position
# MAGIC 2. **extract_fcr_content** (custom function): the full text of the page the best passage is on
# MAGIC 3. A bad call, to show the error the model gets back instead of a crash
# MAGIC
# MAGIC Connects to Lakebase with psycopg's pure-Python build on Databricks (see the first cell).

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build with the system's libpq: its binary build aborted Python
# on serverless compute (docs/08-agent.md). Set before anything imports psycopg.

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger

from fcr_curator.agent_tools import ToolInputError, extract_fcr_content_tool, search_fcr_tool
from fcr_curator.config import load_config
from fcr_curator.vector_search import LakebasePgVectorManager

# COMMAND ----------
# Settings

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

store = LakebasePgVectorManager(cfg, env, WorkspaceClient())
search_fcr = search_fcr_tool(store)
extract_fcr_content = extract_fcr_content_tool(store)

# COMMAND ----------
# 1. search_fcr

passages = json.loads(
    search_fcr.exec_fn(
        query="solvency ratio capital requirement", insurer="Wilton Re", reporting_year=2024, num_results=3
    )
)
for found in passages:
    logger.info(
        f"{found['insurer']} {found['reporting_year']} page {found['page']} [{found['score']}]: {found['excerpt'][:100]!r}"
    )
if not passages:
    raise RuntimeError("search_fcr found nothing for Wilton Re's 2024 solvency position")

# COMMAND ----------
# 2. extract_fcr_content, for the best passage's page

best = passages[0]
page = json.loads(extract_fcr_content.exec_fn(file_url=best["file_url"], page=best["page"]))
logger.info(f"Page {page['page']} of {page['insurer']}'s {page['reporting_year']} FCR, {len(page['text'])} characters:")
logger.info(page["text"][:1000])

# COMMAND ----------
# 3. A bad call: the agent returns this message to the model, which can correct its arguments

try:
    search_fcr.exec_fn(query="solvency", fcr_class="Class 9")
    raise RuntimeError("search_fcr accepted a licence class that doesn't exist")
except ToolInputError as error:
    logger.info(f"Rejected as expected: {error}")
