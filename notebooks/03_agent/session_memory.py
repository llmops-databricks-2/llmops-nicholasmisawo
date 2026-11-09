# Databricks notebook source
# MAGIC %md
# MAGIC # Session memory in Lakebase
# MAGIC
# MAGIC Creates the session table the agent keeps conversations in, `<schema>.agent_session_messages_<env>`
# MAGIC (`memory.py`), then saves a short session and loads it back, checking the order survives.
# MAGIC
# MAGIC Connects to Lakebase with psycopg's pure-Python build on Databricks (see the first cell). Each run
# MAGIC adds one small check session to the table.

# COMMAND ----------
# On Databricks, use psycopg's pure-Python build with the system's libpq: its binary build aborted Python
# on serverless compute (docs/08-agent.md). Set before anything imports psycopg.

import os

if "DATABRICKS_RUNTIME_VERSION" in os.environ:
    os.environ["PSYCOPG_IMPL"] = "python"

# COMMAND ----------

from uuid import uuid4

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger

from fcr_curator.config import load_config
from fcr_curator.memory import LakebaseMemory

# COMMAND ----------
# Settings

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

memory = LakebaseMemory(WorkspaceClient(), cfg, env)
memory.ensure_schema()

# COMMAND ----------
# Save a session, then load it back

session_id = f"job-check-{uuid4()}"
exchange = [
    {"role": "user", "content": "Which years of Wilton Re's FCR do we have?"},
    {"role": "assistant", "content": "2022, 2023 and 2024."},
    {"role": "user", "content": "And its 2024 solvency ratio?"},
]
memory.save_messages(session_id, exchange)
loaded = memory.load_messages(session_id, limit=len(exchange))
logger.info(f"Session {session_id}: saved {len(exchange)} messages, loaded {len(loaded)}")
if loaded != exchange:
    raise RuntimeError(f"Session messages didn't round-trip in order: {loaded}")

last_two = memory.load_messages(session_id, limit=2)
if last_two != exchange[-2:]:
    raise RuntimeError(f"Loading the last 2 messages returned {last_two}")
