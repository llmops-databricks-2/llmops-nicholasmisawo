# Databricks notebook source
# MAGIC %md
# MAGIC # Check the Genie space
# MAGIC
# MAGIC The space is a bundle resource (`resources/genie_space.yml`) over `fcr_register` and
# MAGIC `dim_fcr_entities`. The `fcr_agent` job passes the target's space id as `genie_space_id`; run
# MAGIC interactively, the notebook uses the dev space.
# MAGIC
# MAGIC 1. Ask it a question through the Genie API and show the SQL it wrote
# MAGIC 2. List the tools its managed MCP server offers the agent
# MAGIC
# MAGIC Needs no psycopg.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from loguru import logger

from fcr_curator.config import get_widget
from fcr_curator.mcp import DEV_GENIE_SPACE_ID, genie_mcp_url, mcp_server_tools

# COMMAND ----------
# Settings

SPACE_ID = get_widget("genie_space_id", DEV_GENIE_SPACE_ID)
QUESTION = "How many listed insurers have no FCR yet, by licence class?"

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
