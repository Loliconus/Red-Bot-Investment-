"""Read-only защита SQL-консоли.

Этот тест — не формальность: SQL-консоль в GUI — самый опасный элемент
админки, и именно здесь проверяется, что консоль не превращается в «выполнить
любой запрос».
"""

from __future__ import annotations

import pytest

from adapters.driven.storage.connection_pool import is_select_only


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM trades",
        "select count(*) from candles",
        "  SELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT * FROM trades LIMIT 10;",
    ],
)
def test_readonly_queries_allowed(query: str) -> None:
    assert is_select_only(query)


@pytest.mark.parametrize(
    "query",
    [
        "PRAGMA show_tables",
        "DESCRIBE trades",
        "EXPLAIN SELECT 1",
        "DELETE FROM trades",
        "DROP TABLE trades",
        "UPDATE trades SET verdict = 'loss'",
        "INSERT INTO trades VALUES (1)",
        "ALTER TABLE trades ADD COLUMN x INT",
        "CREATE TABLE evil (x INT)",
        "ATTACH '/etc/passwd' AS p",
        "COPY trades TO 'out.parquet'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM trades WHERE verdict = 'loss'; DROP TABLE trades",
        "TRUNCATE trades",
        "EXPORT DATABASE 'x'",
    ],
)
def test_mutation_queries_blocked(query: str) -> None:
    assert not is_select_only(query)


def test_semicolon_in_middle_blocks_query() -> None:
    """Склейка двух команд через точку с запятой запрещена."""
    assert not is_select_only("SELECT 1; DELETE FROM trades")


def test_private_tables_blocked_at_gui_boundary() -> None:
    from application.use_cases.execute_readonly_query import validate_console_query

    for query in (
        "SELECT * FROM operational_settings",
        "SELECT * FROM portfolio_states",
        "SELECT * FROM ws_replay",
        "SELECT read_text('/etc/passwd')",
    ):
        with pytest.raises(ValueError):
            validate_console_query(query)


def test_empty_query_blocked() -> None:
    assert not is_select_only("")


def test_comment_only_query_blocked() -> None:
    assert not is_select_only("-- привет")
