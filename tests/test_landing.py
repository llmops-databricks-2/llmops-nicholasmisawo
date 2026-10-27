"""Tests for checking that a run's rows match the landed table they're appended to."""

import pytest

from fcr_curator.landing import check_row_columns

COLUMNS = ["query", "read_at"]


def test_rows_with_exactly_the_tables_columns_pass() -> None:
    """Rows whose columns are the table's, in any order, are accepted."""
    check_row_columns("raw_fcr_page_reads", [{"read_at": "2026-10-27", "query": "page:a"}], COLUMNS)


def test_a_row_missing_a_column_fails_naming_it() -> None:
    """A missing column would land as null, so it's an error that names the column and table."""
    with pytest.raises(ValueError, match=r"raw_fcr_page_reads.*missing \['read_at'\]"):
        check_row_columns("raw_fcr_page_reads", [{"query": "page:a"}], COLUMNS)


def test_a_row_with_an_unknown_column_fails_naming_it() -> None:
    """A column the table doesn't define would be dropped silently, so it's an error that names it."""
    with pytest.raises(ValueError, match=r"unknown \['rank'\]"):
        check_row_columns("raw_fcr_page_reads", [{"query": "page:a", "read_at": "2026-10-27", "rank": 1}], COLUMNS)
