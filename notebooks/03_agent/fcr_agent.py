# Databricks notebook source
# MAGIC %md
# MAGIC # The FCR agent: tools and session memory
# MAGIC
# MAGIC The chat model answers questions about Bermuda insurers' FCRs with these tools (`agent_tools.py`):
# MAGIC
# MAGIC | Tool | Technique |
# MAGIC |---|---|
# MAGIC | `search_fcr` | Lakebase pgvector, hybrid search |
# MAGIC | `get_fcr_details` | Unity Catalog function, through managed MCP |
# MAGIC | `extract_fcr_content` | Custom Python function |
# MAGIC | Genie | Genie space, through managed MCP |
# MAGIC
# MAGIC Each session's questions and answers are kept in Lakebase (`memory.py`), so the second question
# MAGIC below, a follow-up that only makes sense after the first, is answered from the saved history.
# MAGIC
# MAGIC Run it after the `fcr_agent` job has registered the UC function and created the session table. Its
# MAGIC widgets, `env` and `genie_space_id`, default to dev and the dev Genie space, so it also runs from your
# MAGIC machine:
# MAGIC
# MAGIC     env -u DATABRICKS_WORKSPACE_ID DATABRICKS_CONFIG_PROFILE=<profile> uv run python notebooks/03_agent/fcr_agent.py

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build with the system's libpq: its binary build aborted Python
# on serverless compute (docs/08-agent.md). Set before anything imports psycopg.

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

from uuid import uuid4

from databricks.sdk import WorkspaceClient
from loguru import logger

from fcr_curator.agent import FCRAgent
from fcr_curator.agent_tools import build_fcr_tools
from fcr_curator.config import get_widget, load_config
from fcr_curator.gateway import gateway_client
from fcr_curator.mcp import DEV_GENIE_SPACE_ID
from fcr_curator.memory import LakebaseMemory

# COMMAND ----------
# Settings

ENV = get_widget("env", "dev")
GENIE_SPACE_ID = get_widget("genie_space_id", DEV_GENIE_SPACE_ID)
QUESTIONS = (
    "How did Wilton Re's solvency compare with its enhanced capital requirement in 2024?",
    "And how did that compare with the year before?",  # Needs the first turn: who, and which measure
)

cfg = load_config("project_config.yml", env=ENV)
workspace = WorkspaceClient()
tools = build_fcr_tools(workspace, cfg, ENV, GENIE_SPACE_ID)
memory = LakebaseMemory(workspace, cfg, ENV)
agent = FCRAgent(gateway_client(workspace), cfg.llm_endpoint, cfg.system_prompt, tuple(tools), memory, cfg.agent)
logger.info(f"Tools: {[tool.name for tool in tools]}")

# COMMAND ----------
# Two turns of one session; the second relies on the history saved by the first

session_id = f"demo-{uuid4()}"
for question in QUESTIONS:
    logger.info(f"\nQ: {question}\n\nA: {agent.answer(question, session_id)}")

# COMMAND ----------
# What the session table now holds for the next turn

for message in memory.load_messages(session_id, cfg.agent.history_messages):
    logger.info(f"{message['role']}: {message['content'][:120]!r}")
