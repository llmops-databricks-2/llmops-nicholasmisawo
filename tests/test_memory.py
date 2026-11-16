"""Tests for keeping session messages in Lakebase: table naming, order and what's sent to Postgres."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from fcr_curator import memory as memory_module
from fcr_curator.config import ProjectConfig
from fcr_curator.memory import LakebaseMemory

CONFIG = ProjectConfig.from_yaml(str(Path(__file__).resolve().parents[1] / "project_config.yml"), "dev")
MESSAGES = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]


class FakeConnection:
    """Records statements; queries return the given rows."""

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.executed: list[tuple[object, object]] = []

    def __enter__(self) -> "FakeConnection":
        """Open the connection, as psycopg's does in a with block."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection; there's nothing to close."""

    def execute(self, query: object, params: object = None) -> SimpleNamespace:
        """Record the statement and return a cursor holding the rows."""
        self.executed.append((query, params))
        return SimpleNamespace(fetchall=lambda: self.rows)

    def cursor(self) -> "FakeConnection":
        """Return itself as the cursor, so executemany is recorded too."""
        return self

    def executemany(self, query: object, params: list) -> None:
        """Record the batch."""
        self.executed.append((query, params))


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    """Replace the Lakebase connection with a fake holding MESSAGES as rows."""
    fake = FakeConnection([(message,) for message in MESSAGES])
    monkeypatch.setattr(memory_module, "connect", lambda workspace, config: fake)
    return fake


def test_each_environment_keeps_sessions_in_its_own_table() -> None:
    """dev, acc and prd share one Lakebase database, so the environment is in the table's name."""
    assert LakebaseMemory(SimpleNamespace(), CONFIG, "acc").table_name == "agent_session_messages_acc"


def test_load_messages_asks_for_the_last_messages_oldest_first(connection: FakeConnection) -> None:
    """The newest `limit` messages are picked by id, then returned in the order they were saved."""
    loaded = LakebaseMemory(SimpleNamespace(), CONFIG).load_messages("session-1", limit=20)

    [(query, params)] = connection.executed
    statement = query.as_string(None)
    assert "ORDER BY id DESC LIMIT %s) AS recent ORDER BY id" in statement
    assert params == ["session-1", 20]
    assert loaded == MESSAGES


def test_save_messages_inserts_every_message_in_one_batch_as_jsonb(connection: FakeConnection) -> None:
    """Messages keep their order, each as JSONB, in one transaction."""
    LakebaseMemory(SimpleNamespace(), CONFIG).save_messages("session-1", MESSAGES)

    [(_, rows)] = connection.executed
    assert [session for session, _ in rows] == ["session-1", "session-1"]
    assert all(isinstance(data, Jsonb) for _, data in rows)
    assert [data.obj for _, data in rows] == MESSAGES


def test_ensure_schema_creates_the_schema_table_and_session_index(connection: FakeConnection) -> None:
    """The table is created in the project's schema with an index for loading one session."""
    LakebaseMemory(SimpleNamespace(), CONFIG).ensure_schema()

    statements = [query.as_string(None) for query, _ in connection.executed]
    assert statements[0] == 'CREATE SCHEMA IF NOT EXISTS "nicholasmisawo"'
    assert '"nicholasmisawo"."agent_session_messages_dev"' in statements[1]
    assert "(session_id, id)" in statements[2]
