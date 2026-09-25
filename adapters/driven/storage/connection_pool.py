"""Пул подключений к DuckDB.

Файловый (не in-memory) режим — осознанный выбор: база переживает перезапуск
процесса и не требует отдельного сервера.

Обязательные настройки, без которых DuckDB на аналитических объёмах выедает
всю память и кладёт процесс:

* ``memory_limit`` — жёсткий потолок; при нехватке DuckDB начинает спиллить
  на диск, а не падает с OOM;
* ``temp_directory`` — куда спиллить; по умолчанию рядом с БД, чтобы не
  забивать системный ``/tmp``;
* ``threads`` — ограничение параллелизма, иначе DuckDB занимает все ядра
  и конкурирует с торговым циклом.

DuckDB-коннекшен не потокобезопасен, поэтому он один, а все запросы
выполняются под блокировкой и выносятся в отдельный поток через
``asyncio.to_thread`` — так тяжёлые запросы не блокируют event loop.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Запросы, которые разрешены в read-only SQL-консоли GUI.
SELECT_ONLY_PATTERN = re.compile(
    r"^\s*(SELECT|WITH|PRAGMA|DESCRIBE|EXPLAIN|SHOW|SUMMARIZE)\b",
    re.IGNORECASE | re.DOTALL,
)

#: Конструкции, опасные в пользовательской консоли.
FORBIDDEN_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|COPY|INSTALL|LOAD|EXPORT|IMPORT)\b",
    re.IGNORECASE,
)


def is_select_only(query: str) -> bool:
    """Пропускает только read-only запросы.

    Проверка двухуровневая: запрос обязан начинаться с read-only ключевого
    слова и не содержать ни одной мутирующей конструкции. Точки с запятой
    запрещены — это отсекает попытку приклеить вторую команду.
    """
    stripped = query.strip().rstrip(";")
    if ";" in stripped:
        return False
    if not SELECT_ONLY_PATTERN.match(stripped):
        return False
    return FORBIDDEN_PATTERN.search(stripped) is None


class DuckDBConnectionPool:
    """Один коннекшен + блокировка + вынос запросов в поток."""

    __slots__ = ("_conn", "_db_path", "_lock", "_settings_applied")

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
        self._settings_applied = False
        self._apply_settings(memory_limit_mb, threads, temp_directory)

    def _apply_settings(
        self, memory_limit_mb: int, threads: int, temp_directory: Path | None
    ) -> None:
        temp = temp_directory or (self._db_path.parent / "duckdb_temp")
        temp.mkdir(parents=True, exist_ok=True)

        self._conn.execute(f"SET memory_limit='{memory_limit_mb}MB'")
        self._conn.execute(f"SET threads={threads}")
        self._conn.execute(f"SET temp_directory='{temp.as_posix()}'")
        self._conn.execute("SET enable_progress_bar=false")
        self._settings_applied = True

        logger.info(
            "duckdb_configured",
            path=str(self._db_path),
            memory_limit_mb=memory_limit_mb,
            threads=threads,
            temp_directory=str(temp),
        )

    @property
    def db_path(self) -> Path:
        return self._db_path

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        """Синхронное выполнение запроса. Потокобезопасно."""
        with self._lock:
            if params:
                self._conn.execute(sql, list(params))
            else:
                self._conn.execute(sql)
            rows: list[tuple[Any, ...]] = self._conn.fetchall()
            return rows

    def execute_script(self, sql: str) -> None:
        """Выполняет скрипт из нескольких statements (DDL)."""
        with self._lock:
            self._conn.execute(sql)

    def query_readonly(self, sql: str) -> list[tuple[Any, ...]]:
        """Read-only запрос для SQL-консоли GUI. Кидает ValueError на мутацию."""
        if not is_select_only(sql):
            msg = "Разрешены только read-only запросы (SELECT/WITH/PRAGMA/DESCRIBE/EXPLAIN)"
            raise ValueError(msg)
        return self.execute(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> DuckDBConnectionPool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
