# Databricks notebook source
# MAGIC %md
# MAGIC # Register the agent's UC function: get_fcr_details
# MAGIC
# MAGIC Creates or replaces `<catalog>.<schema>.get_fcr_details(insurer)` (`uc_functions.py`): the FCRs
# MAGIC registered for one insurer, newest first, as a JSON array. Then calls it two ways:
# MAGIC
# MAGIC 1. **SQL**, on the warehouse, as any Unity Catalog user with EXECUTE could
# MAGIC 2. **MCP**, through the managed server for UC functions, the way the agent calls it
# MAGIC
# MAGIC Runs on the warehouse through the Statement Execution API, so it needs no psycopg.

# COMMAND ----------

import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from databricks.sdk.service.sql import StatementParameterListItem
from loguru import logger

from fcr_curator.config import load_config
from fcr_curator.mcp import mcp_server_tools
from fcr_curator.uc_functions import GET_FCR_DETAILS, register_uc_functions, uc_function_mcp_url

# COMMAND ----------
# Settings

dbutils.widgets.text("env", "dev")
cfg = load_config("../../project_config.yml", env=dbutils.widgets.get("env"))
INSURER = "Wilton Re"  # Registered as Wilton Reinsurance Bermuda Limited, with FCRs for 2022 to 2024

workspace = WorkspaceClient()

# COMMAND ----------
# Register

[function] = register_uc_functions(workspace, cfg)

# COMMAND ----------
# 1. Call it through SQL

result = workspace.statement_execution.execute_statement(
    statement=f"SELECT {function}(:insurer) AS fcrs",
    parameters=[StatementParameterListItem(name="insurer", value=INSURER)],
    warehouse_id=cfg.warehouse_id,
    wait_timeout="50s",
)
fcrs = json.loads(result.result.data_array[0][0])
logger.info(f"SQL: {len(fcrs)} FCRs for {INSURER}: {[fcr['reporting_year'] for fcr in fcrs]}")
if not fcrs:
    raise RuntimeError(f"{function} found no FCRs for {INSURER}")

# COMMAND ----------
# 2. Call it through its MCP server, as the agent does

[uc_tool] = mcp_server_tools(workspace, uc_function_mcp_url(workspace.config.host, cfg, GET_FCR_DETAILS))
reply = uc_tool.exec_fn(insurer=INSURER)
logger.info(f"MCP tool {uc_tool.name} replied: {reply[:500]}")
if "Wilton" not in reply:
    raise RuntimeError(f"The MCP tool {uc_tool.name} didn't return {INSURER}'s FCRs: {reply[:500]}")
