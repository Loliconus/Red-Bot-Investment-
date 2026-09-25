"""Единственная точка входа для ad-hoc SQL-консоли GUI.

Разрешён один SELECT (включая WITH ... SELECT) над публичными таблицами бота.
Дополнительная проверка в RepositoryPort защищает от ошибки вызывающего кода.
Денежные значения сохраняются Decimal/строками, никогда не float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from application.composition import AppContext

# Таблицы с идентификатором счёта, служебными настройками и секретами не
# доступны даже через SELECT *. Аудит содержит только замаскированные поля.
PUBLIC_TABLES = frozenset(
    {
        "instruments",
        "candles",
        "orderbook_snapshots",
        "market_snapshots",
        "decision_snapshots",
        "trade_plans",
        "trades",
        "hypotheses",
        "strategy_configs",
        "gui_audit",
    }
)
SAFE_FUNCTIONS = frozenset(
    {
        "ABS",
        "AVG",
        "CAST",
        "COALESCE",
        "COUNT",
        "DATE_TRUNC",
        "LENGTH",
        "LOWER",
        "MAX",
        "MIN",
        "NULLIF",
        "ROUND",
        "STRFTIME",
        "SUBSTRING",
        "SUM",
        "TRIM",
        "TRY_CAST",
        "UPPER",
    }
)
MAX_ROWS = 1000


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def validate_console_query(query: str) -> str:
    """Возвращает канонический SELECT или бросает ValueError ДО обращения к порту."""
    text = query.strip().removesuffix(";").rstrip()
    if not text or any(marker in text for marker in (";", "--", "/*", "*/")):
        raise ValueError("Разрешён только один запрос SELECT или WITH без комментариев")
    try:
        statements = sqlglot.parse(text, read="duckdb", error_level=ErrorLevel.RAISE)
    except (sqlglot.ParseError, ValueError) as exc:
        raise ValueError("Некорректный SELECT-запрос") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Разрешены только SELECT или WITH ... SELECT")
    statement = statements[0]
    if any(
        isinstance(node, (exp.Command, exp.DDL, exp.DML, exp.Into)) for node in statement.walk()
    ):
        raise ValueError("Изменяющие запросы и SELECT INTO запрещены")
    ctes = {cte.alias.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if (
            not isinstance(table.this, exp.Identifier)
            or table.db
            or table.catalog
            or table.name.lower() not in PUBLIC_TABLES | ctes
        ):
            raise ValueError("Доступны только публичные таблицы бота, без файлов и внешних БД")
    if any(func.sql_name().upper() not in SAFE_FUNCTIONS for func in statement.find_all(exp.Func)):
        raise ValueError("Произвольные SQL-функции запрещены в консоли")
    return statement.sql(dialect="duckdb")


async def execute_readonly_query(
    context: AppContext, query: str, *, row_limit: int = MAX_ROWS
) -> QueryResult:
    if not 1 <= row_limit <= MAX_ROWS:
        raise ValueError("LIMIT должен быть между 1 и 1000")
    sql = validate_console_query(query)
    # Внешний лимит действует и для пользовательского запроса без LIMIT, и для
    # SELECT с явным большим LIMIT. +1 нужен, чтобы показать усечение.
    columns, rows = await context.repository.read_query(
        f"SELECT * FROM ({sql}) AS redbot_console_result LIMIT {row_limit + 1}"  # noqa: S608 — AST-валидированный SELECT
    )
    return QueryResult(
        columns=columns,
        rows=[[serialize_cell(cell) for cell in row] for row in rows[:row_limit]],
        truncated=len(rows) > row_limit,
    )


def serialize_cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value
