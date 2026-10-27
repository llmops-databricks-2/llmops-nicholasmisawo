# Databricks notebook source
# MAGIC %md
# MAGIC # Sync the knowledge base to Lakebase
# MAGIC
# MAGIC Copies dbt's `fcr_knowledge_base` (chunks, company, year, page citation and embedding) into the
# MAGIC Lakebase Postgres database as `<schema>.fcr_knowledge_base_lakebase_<env>`, with `embedding` as a pgvector
# MAGIC `vector(1024)` column, keyed by `source_key`.
# MAGIC
# MAGIC First run: create the synced table, which copies every row. Later runs: refresh it, copying only
# MAGIC what changed, and wait for it to finish (`lakebase.py`). A failed sync fails this task.
# MAGIC
# MAGIC One-time setup, already done: the pgvector extension is installed in the Lakebase database
# MAGIC (`lakebase.ensure_vector_extension`, run from a laptop; every environment shares the database).
# MAGIC
# MAGIC Uses no SerpAPI searches and no embedding calls.

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.runtime import dbutils
from loguru import logger

from fcr_curator.config import load_config
from fcr_curator.lakebase import sync_to_lakebase, synced_table_name, synced_table_spec
from fcr_curator.tables import KNOWLEDGE_BASE_TABLE

# COMMAND ----------
# Settings

dbutils.widgets.text("env", "dev")
env = dbutils.widgets.get("env")
cfg = load_config("../../project_config.yml", env=env)

SOURCE_TABLE = f"{cfg.full_schema_name}.{KNOWLEDGE_BASE_TABLE}"
SYNCED_TABLE = synced_table_name(cfg, env)

# COMMAND ----------
# Create or refresh the synced table

outcome = sync_to_lakebase(WorkspaceClient(), SYNCED_TABLE, synced_table_spec(cfg, SOURCE_TABLE))
logger.info(f"{SYNCED_TABLE}: {outcome}")
