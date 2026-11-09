"""Tests for searching the Lakebase knowledge base from a question with LakebasePgVectorManager."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fcr_curator import vector_search
from fcr_curator.config import ProjectConfig
from fcr_curator.gateway import EmbedBatch
from fcr_curator.search import TEXT_SEARCH_VECTOR, SearchFilters
from fcr_curator.vector_search import LakebasePgVectorManager

CONFIG = ProjectConfig.from_yaml(str(Path(__file__).resolve().parents[1] / "project_config.yml"), "dev")
QUERY_VECTOR = [0.1, 0.2, 0.3]
ROW = ("key-1", "BSCR ratio of 250%", "Wilton Re", 2024, 15, "https://wiltonre.bm/fcr.pdf#page=15", "Class 4", 0.87)


class FakeConnection:
    """Records each statement executed and returns ROW for every query."""

    def __init__(self) -> None:
        self.executed: list[tuple[object, list | None]] = []

    def __enter__(self) -> "FakeConnection":
        """Open the connection, as psycopg's does in a with block."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the connection; there's nothing to close."""
        return None

    def execute(self, query: object, params: list | None = None) -> SimpleNamespace:
        """Record the statement and return a cursor holding ROW."""
        self.executed.append((query, params))
        return SimpleNamespace(fetchall=lambda: [ROW])


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    """Replace the Lakebase connection with a fake, returned so tests can see what was executed."""
    fake = FakeConnection()
    monkeypatch.setattr(vector_search, "connect", lambda workspace, config: fake)
    return fake


def manager(
    embed: EmbedBatch = lambda texts: [QUERY_VECTOR for _ in texts], env: str = "dev"
) -> LakebasePgVectorManager:
    """Build a manager whose embedding model returns QUERY_VECTOR, with no workspace behind it."""
    return LakebasePgVectorManager(CONFIG, env, workspace=SimpleNamespace(), embed=embed)


def test_the_manager_searches_its_environments_synced_table_without_the_catalog() -> None:
    """Postgres names the table <schema>.<table>, and the environment keeps dev, acc and prd apart."""
    assert manager(env="acc").table == "nicholasmisawo.fcr_knowledge_base_lakebase_acc"


def test_embed_query_embeds_only_the_query() -> None:
    """The query is embedded on its own, and its vector returned as the model gave it."""
    embedded: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float]]:
        embedded.append(texts)
        return [QUERY_VECTOR]

    assert manager(embed).embed_query("solvency ratio") == QUERY_VECTOR
    assert embedded == [["solvency ratio"]]


def test_embed_query_fails_when_the_model_returns_an_empty_vector() -> None:
    """An empty vector can't be compared with the chunks', so it's an error rather than no results."""
    with pytest.raises(ValueError, match="returned no vector"):
        manager(lambda texts: [[]]).embed_query("solvency ratio")


def test_search_compares_the_query_vector_within_the_filters(connection: FakeConnection) -> None:
    """Semantic search sends the filter values and the query's vector, and returns citable hits."""
    [hit] = manager().search("solvency ratio", SearchFilters(reporting_year=2024), limit=5)

    [(_, params)] = connection.executed
    assert params == [str(QUERY_VECTOR), 2024, str(QUERY_VECTOR), 5]
    assert (hit.company, hit.page, hit.fcr_class, hit.score) == ("Wilton Re", 15, "Class 4", 0.87)


def test_hybrid_search_runs_semantic_and_keyword_search_and_fuses_them(connection: FakeConnection) -> None:
    """Both searches run, and a chunk both return appears once."""
    hits = manager().hybrid_search("solvency ratio")

    assert len(connection.executed) == 2
    assert [hit.id for hit in hits] == ["key-1"]


def test_ensure_indexes_indexes_the_expression_keyword_search_matches(connection: FakeConnection) -> None:
    """Postgres uses an expression index only for that exact expression, so both come from one constant."""
    manager().ensure_indexes()

    [(statement, _)] = connection.executed
    created = statement.as_string(None)
    assert f"USING gin (({TEXT_SEARCH_VECTOR}))" in created
    assert '"fcr_knowledge_base_lakebase_dev_text_gin"' in created


def test_page_reads_one_cited_pages_chunks_in_reading_order(connection: FakeConnection) -> None:
    """A page is looked up by its exact citation URL, ordered by the chunk number in source_id."""
    citation = "https://wiltonre.bm/fcr.pdf#page=15"

    [chunk] = manager().page(citation)

    [(query, params)] = connection.executed
    assert params == [citation]
    assert "ORDER BY (regexp_replace(source_id, '^.*_', ''))::int" in query.as_string(None)
    assert chunk.page == 15
