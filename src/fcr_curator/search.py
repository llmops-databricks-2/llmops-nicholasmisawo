"""Search the Lakebase copy of the FCR knowledge base: semantic, keyword and hybrid, each with filters.

The table has the dbt_context_engineering knowledge_base shape: source_key, text, account_key (the
company), ts (31 December of the reporting year), citation_url (the FCR opened at the chunk's page),
classification (the insurer's FCR licence class) and embedding.


- semantic: pgvector cosine distance (<=>). The embeddings aren't normalised, so cosine, not inner product.
- keyword: Postgres full-text search for any of the query's words, ranked by ts_rank_cd. Catches exact
  terms (BSCR, ECR, a figure) that embeddings blur.
- hybrid: both, fused with Reciprocal Rank Fusion, which needs only ranks, not comparable scores.

The synced table belongs to the sync, so it has no search indexes. Exact scans over ~20k chunks take
about 0.1s when filtered and a few seconds for an unfiltered keyword search.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from psycopg import sql

from fcr_curator.curation import institution_key

DEFAULT_LIMIT = 20
RRF_K = 60  # The usual Reciprocal Rank Fusion constant: damps the weight of the very top ranks
# The page is read back from the citation's #page=N, so a hit and its citation can't disagree
HIT_COLUMNS = sql.SQL(
    "source_key, text, account_key, extract(year from ts)::int,"
    " substring(citation_url from '#page=([0-9]+)$')::int, citation_url, classification"
)
# The licence classes on the entity list (in_search_scope in dbt/seeds/bma_licence_classes.csv): the
# values classification takes. Class 4 and 3B write general business, Long Term Class E long-term
FCR_CLASSES = ("Class 4", "Long Term Class E", "Class 3B", "Group")


@dataclass(frozen=True)
class SearchHit:
    """One chunk a search returned, with what's needed to cite it."""

    id: str  # The knowledge base's source_key
    text: str
    company: str | None
    reporting_year: int | None
    page: int | None  # As printed in the PDF, counted from 1
    citation_url: str | None  # The FCR, opened at that page
    fcr_class: str | None = None  # The insurer's FCR licence class, e.g. "Class 4"


@dataclass(frozen=True)
class SearchFilters:
    """Metadata a search is limited to; None means any."""

    institution: str | None = None
    reporting_year: int | None = None
    fcr_class: str | None = None  # One of FCR_CLASSES

    def where(self) -> tuple[sql.Composable, list]:
        """Return the SQL condition for these filters and its parameters."""
        conditions, params = [sql.SQL("TRUE")], []
        if self.institution:
            # The company name isn't always clean (wiltonre.bm), so the key is matched against the company
            # and the citation URL, which holds the domain and often the company in its filename
            conditions.append(
                sql.SQL(
                    "regexp_replace(lower(concat_ws(' ', account_key, citation_url)), '[^a-z0-9]', '', 'g') LIKE %s"
                )
            )
            params.append(f"%{institution_key(self.institution)}%")
        if self.reporting_year:
            conditions.append(sql.SQL("extract(year from ts) = %s"))
            params.append(self.reporting_year)
        if self.fcr_class:
            conditions.append(sql.SQL("classification = %s"))
            params.append(self.fcr_class)
        return sql.SQL(" AND ").join(conditions), params


NO_FILTERS = SearchFilters()


def any_word_query(text: str) -> str:
    """Return text as a websearch_to_tsquery matching any of its words: 'BSCR ratio' -> 'BSCR or ratio'."""
    return " or ".join(re.findall(r"\w+", text))


def reciprocal_rank_fusion(rankings: list[list[SearchHit]], limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
    """Merge rankings into one: each hit scores the sum of 1 / (RRF_K + rank) over the rankings it's in."""
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.id] += 1 / (RRF_K + rank)
            hits[hit.id] = hit
    return [hits[hit_id] for hit_id in sorted(scores, key=scores.get, reverse=True)][:limit]


class ChunkSearch:
    """Searches the synced knowledge base table over an open Lakebase connection."""

    def __init__(self, connection: psycopg.Connection, table: str) -> None:
        self.connection = connection
        self.table = sql.Identifier(*table.split("."))  # "<schema>.<table>"

    def _hits(self, query: sql.Composable, params: list) -> list[SearchHit]:
        return [SearchHit(*row) for row in self.connection.execute(query, params).fetchall()]

    def semantic(
        self, query_vector: list[float], filters: SearchFilters = NO_FILTERS, limit: int = DEFAULT_LIMIT
    ) -> list[SearchHit]:
        """Return the chunks whose embeddings are closest to query_vector."""
        where, params = filters.where()
        query = sql.SQL("SELECT {columns} FROM {table} WHERE {where} ORDER BY embedding <=> %s::vector LIMIT %s")
        return self._hits(
            query.format(columns=HIT_COLUMNS, table=self.table, where=where), [*params, str(query_vector), limit]
        )

    def keyword(
        self, query_text: str, filters: SearchFilters = NO_FILTERS, limit: int = DEFAULT_LIMIT
    ) -> list[SearchHit]:
        """Return the chunks containing any of query_text's words, best matches first."""
        where, params = filters.where()
        query = sql.SQL(
            "SELECT {columns} FROM {table}, websearch_to_tsquery('english', %s) AS words"
            " WHERE {where} AND to_tsvector('english', text) @@ words"
            " ORDER BY ts_rank_cd(to_tsvector('english', text), words) DESC LIMIT %s"
        )
        return self._hits(
            query.format(columns=HIT_COLUMNS, table=self.table, where=where),
            [any_word_query(query_text), *params, limit],
        )

    def hybrid(
        self,
        query_text: str,
        query_vector: list[float],
        filters: SearchFilters = NO_FILTERS,
        limit: int = DEFAULT_LIMIT,
    ) -> list[SearchHit]:
        """Return the semantic and keyword results fused into one ranking."""
        rankings = [self.semantic(query_vector, filters, limit), self.keyword(query_text, filters, limit)]
        return reciprocal_rank_fusion(rankings, limit)
