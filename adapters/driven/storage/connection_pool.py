"""Сериализованный пул DuckDB и защитный барьер read-only запросов.

SQL-консоль работает через RepositoryPort; эта проверка повторяется и здесь,
чтобы ошибочный вызывающий код также не мог превратить SELECT в мутацию.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sqlglot
import structlog
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from adapters.driven.storage.schema import TABLES

logger = structlog.get_logger(__name__)

# Не разрешаем произвольные table functions, UDF и вызовы query()/read_*().
# Даже SELECT read_text('/etc/...') не должен быть допустим из GUI.
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


def is_select_only(query: str) -> bool:
    """Один запрос SELECT/WITH→SELECT над локальными таблицами, без побочных эффектов.

    Проверяем AST, а не префикс регулярным выражением: вложенные DML, SELECT
    INTO, table functions и несколько statements должны отклоняться. Имена CTE
    допустимы, но обращение к файлам/каталогам/схемам — нет.
    """
    text = query.strip().removesuffix(";").rstrip()
    if not text or any(marker in text for marker in (";", "--", "/*", "*/")):
        return False
    try:
        statements = sqlglot.parse(text, read="duckdb", error_level=ErrorLevel.RAISE)
    except (sqlglot.ParseError, ValueError):
        return False
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return False
    statement = statements[0]
    if any(
        isinstance(node, (exp.Command, exp.DDL, exp.DML, exp.Into)) for node in statement.walk()
    ):
        return False
    cte_names = {cte.alias.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if (
            not isinstance(table.this, exp.Identifier)
            or table.db
            or table.catalog
            or table.name.lower() not in set(TABLES) | cte_names
        ):
            return False
    return all(func.sql_name().upper() in SAFE_FUNCTIONS for func in statement.find_all(exp.Func))


class DuckDBConnectionPool:
    """Один коннекшен + блокировка + вынос вызывающим кодом в asyncio.to_thread."""

    __slots__ = ("_conn", "_db_path", "_lock")

    def __init__(
        self,
        db_path: Path,
        *,
        memory_limit_mb: int = 1536,
        threads: int = 2,
        temp_directory: Path | None = None,
    ) -> None:
        import duckdb

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: Any = duckdb.connect(str(db_path))
        temp = temp_directory or (db_path.parent / "duckdb_temp")
        temp.mkdir(parents=True, exist_ok=True)
        self._conn.execute(f"SET memory_limit='{memory_limit_mb}MB'")
        self._conn.execute(f"SET threads={threads}")
        self._conn.execute(f"SET temp_directory='{temp.as_posix()}'")
        self._conn.execute("SET enable_progress_bar=false")
        logger.info("duckdb_configured", path=str(db_path), memory_limit_mb=memory_limit_mb)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        with self._lock:
            self._conn.execute(sql, list(params) if params is not None else [])
            return list(self._conn.fetchall())

    def execute_script(self, sql: str) -> None:
        with self._lock:
            self._conn.execute(sql)

    def query_readonly(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        return self.query_readonly_with_columns(sql, params)[1]

    def query_readonly_with_columns(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        if not is_select_only(sql):
            msg = "Разрешён один SELECT/WITH над локальными таблицами; запись и внешние функции запрещены"
            raise ValueError(msg)
        with self._lock:
            self._conn.execute(sql, list(params) if params is not None else [])
            columns = [column[0] for column in self._conn.description]
            return columns, list(self._conn.fetchall())

    def restore_tables(self, paths: dict[str, Path]) -> None:
        """Целиком заменяет таблицы из заранее проверенных файлов в одной транзакции."""
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            try:
                for table, path in paths.items():
                    # Таблицы выбираются по TABLES; пути разрешаются вызывающим use case.
                    if table not in TABLES:
                        raise ValueError("Неизвестная таблица в резервной копии")
                    quoted = path.as_posix().replace("'", "''")
                    self._conn.execute(f"DELETE FROM {table}")
                    self._conn.execute(
                        f"INSERT INTO {table} SELECT * FROM read_parquet('{quoted}')"
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_memory_limit_mb(self, limit_mb: int) -> None:
        if not 128 <= limit_mb <= 16384:
            raise ValueError("memory_limit: от 128 до 16384 MB")
        with self._lock:
            self._conn.execute(f"SET memory_limit='{limit_mb}MB'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> DuckDBConnectionPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
