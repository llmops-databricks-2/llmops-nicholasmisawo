# Databricks notebook source
# MAGIC %md
# MAGIC # Check the Genie space
# MAGIC
# MAGIC The space is a bundle resource (`resources/genie_space.yml`) over `fcr_register` and
# MAGIC `dim_fcr_entities`. The `fcr_agent` job passes the deployed space's id as `genie_space_id`.
# MAGIC
# MAGIC 1. Ask it a question through the Genie API and show the SQL it wrote
# MAGIC 2. List the tools its managed MCP server offers the agent
# MAGIC
# MAGIC Needs no psycopg.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger

from fcr_curator.mcp import genie_mcp_url, mcp_server_tools

# COMMAND ----------
# Settings

dbutils.widgets.text("genie_space_id", "")
SPACE_ID = dbutils.widgets.get("genie_space_id").strip()
QUESTION = "How many listed insurers have no FCR yet, by licence class?"
if not SPACE_ID:
    raise ValueError("Set genie_space_id: run this through the fcr_agent job, which passes the deployed space's id")

workspace = WorkspaceClient()

# COMMAND ----------
# 1. Ask the space a question

space = workspace.genie.get_space(space_id=SPACE_ID)
logger.info(f"Genie space {space.title} ({space.space_id}) on warehouse {space.warehouse_id}")

message = workspace.genie.start_conversation_and_wait(space_id=SPACE_ID, content=QUESTION)
for attachment in message.attachments or []:
    if attachment.query:
        logger.info(f"Genie's SQL: {attachment.query.query}")
    if attachment.text:
        logger.info(f"Genie's answer: {attachment.text.content}")
if message.error:
    raise RuntimeError(f"Genie couldn't answer {QUESTION!r}: {message.error}")

# COMMAND ----------
# 2. The tools its MCP server offers the agent

genie_tools = mcp_server_tools(workspace, genie_mcp_url(workspace.config.host, SPACE_ID))
logger.info(f"Genie MCP tools: {[tool.name for tool in genie_tools]}")
if not genie_tools:
    raise RuntimeError(f"The Genie space {SPACE_ID}'s MCP server offers no tools")
