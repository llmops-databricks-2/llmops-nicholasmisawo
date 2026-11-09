"""Keep each agent session's messages in Lakebase, so the next turn, or a restarted process, continues it.

One row per message in <schema>.agent_session_messages_<env>, next to the knowledge base's synced
table. Every environment shares one Lakebase database, so the environment is in the name, as it is for
the synced table.

- Messages come back ordered by id, not created_at: CURRENT_TIMESTAMP is the transaction's start time,
  so messages saved together share one and could come back in any order.
- Each call opens its own connection (lakebase.connect), so its OAuth token is always fresh; a pool
  would keep using connections whose token expires after about an hour.
- Errors propagate: a turn that can't load or save its history fails, rather than continuing without it.
"""

from typing import Any

from databricks.sdk import WorkspaceClient
from loguru import logger
from psycopg import sql
from psycopg.types.json import Jsonb

from fcr_curator.config import ProjectConfig
from fcr_curator.lakebase import connect

SESSION_TABLE = "agent_session_messages"


class LakebaseMemory:
    """Saves and loads a session's messages in the environment's Lakebase session table."""

    def __init__(self, workspace: WorkspaceClient, config: ProjectConfig, env: str = "dev") -> None:
        """Keep env's sessions in <schema>.agent_session_messages_<env>."""
        self.workspace = workspace
        self.config = config
        self.table_name = f"{SESSION_TABLE}_{env}"
        self.table = sql.Identifier(config.schema, self.table_name)

    def ensure_schema(self) -> None:
        """Create the session table and its index if they're missing; does nothing if they exist."""
        statements = [
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=sql.Identifier(self.config.schema)),
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {table} (id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL,"
                " message_data JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            ).format(table=self.table),
            sql.SQL("CREATE INDEX IF NOT EXISTS {index} ON {table} (session_id, id)").format(
                index=sql.Identifier(f"{self.table_name}_session"), table=self.table
            ),
        ]
        with connect(self.workspace, self.config) as connection:
            for statement in statements:
                connection.execute(statement)
        logger.info(f"Session table {self.config.schema}.{self.table_name} ready")

    def load_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        """Return the session's last `limit` messages, oldest first; [] for a new session."""
        query = sql.SQL(
            "SELECT message_data FROM (SELECT id, message_data FROM {table} WHERE session_id = %s"
            " ORDER BY id DESC LIMIT %s) AS recent ORDER BY id"
        ).format(table=self.table)
        with connect(self.workspace, self.config) as connection:
            return [row[0] for row in connection.execute(query, [session_id, limit]).fetchall()]

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Append messages to the session, in order, in one transaction."""
        insert = sql.SQL("INSERT INTO {table} (session_id, message_data) VALUES (%s, %s)").format(table=self.table)
        with connect(self.workspace, self.config) as connection, connection.cursor() as cursor:
            cursor.executemany(insert, [(session_id, Jsonb(message)) for message in messages])
