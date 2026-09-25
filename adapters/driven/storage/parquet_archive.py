"""Холодный архив: Parquet + ZSTD.

Почему Parquet: колоночный формат со сжатием даёт существенный выигрыш по
месту на снапшотах (а их больше всего) и при этом читается DuckDB напрямую,
без загрузки обратно в БД — запрос вида ``read_parquet('archive/*.parquet')``
работает с холодными данными так же, как с таблицей.

ZSTD включён потому, что даёт лучший компромисс «степень сжатия / скорость
чтения» по сравнению с gzip и snappy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from adapters.driven.storage.connection_pool import DuckDBConnectionPool
from adapters.driven.storage.schema import TABLES

logger = structlog.get_logger(__name__)

ARCHIVABLE_TABLES: tuple[str, ...] = (
    "market_snapshots",
    "decision_snapshots",
    "orderbook_snapshots",
    "candles",
)

#: Имя колонки времени у каждой таблицы: оно различается, и это легко забыть.
# Журнал аудита — append-only и не заменяется старой копией; оперативный
# managed_account_id также не должен тихо меняться при restore.
BACKUP_TABLES = tuple(t for t in TABLES if t not in {"gui_audit", "operational_settings"})

TIME_COLUMN: dict[str, str] = {
    "market_snapshots": "captured_at",
    "decision_snapshots": "created_at",
    "orderbook_snapshots": "captured_at",
    "candles": "ts",
}


class ParquetArchive:
    """Реализация ``ArchivePort``: перенос данных в Parquet и чтение обратно."""

    __slots__ = ("_archive_dir", "_batch_size", "_pool")

    def __init__(
        self,
        *,
        pool: DuckDBConnectionPool | None = None,
        repository: Any = None,
        archive_dir: Path | str,
        batch_size: int = 50_000,
    ) -> None:
        if pool is None and repository is None:
            msg = "Нужен либо pool, либо repository"
            raise ValueError(msg)
        self._pool = pool or repository._pool  # noqa: SLF001 - пул один на процесс
        self._archive_dir = Path(archive_dir)
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._batch_size = batch_size

    @property
    def archive_dir(self) -> Path:
        return self._archive_dir

    async def archive_snapshots(self, older_than: datetime) -> int:
        """Переносит снапшоты старше даты в Parquet и удаляет из горячего слоя."""
        total = 0
        stamp = older_than.strftime("%Y%m%d")

        for table in ARCHIVABLE_TABLES:
            target = self._archive_dir / f"{table}_{stamp}.parquet"
            total += await asyncio.to_thread(self._archive_table, table, target, older_than)

        logger.info("archive_completed", rows=total, older_than=older_than.isoformat())
        return total

    def _archive_table(self, table: str, target: Path, older_than: datetime) -> int:
        column = TIME_COLUMN[table]
        copy_sql = (
            f"COPY (SELECT * FROM {table} WHERE {column} < ?) "
            f"TO '{target.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        self._pool.execute(copy_sql, [older_than])
        rows = self._pool.execute(f"SELECT count(*) FROM {table} WHERE {column} < ?", [older_than])
        count = int(rows[0][0]) if rows else 0
        if count:
            self._pool.execute(f"DELETE FROM {table} WHERE {column} < ?", [older_than])
        return count

    async def archive_candles(self, older_than: datetime, *, timeframe: str) -> int:
        """Архивация свечей конкретного таймфрейма."""
        stamp = older_than.strftime("%Y%m%d")
        target = self._archive_dir / f"candles_{timeframe}_{stamp}.parquet"
        return await asyncio.to_thread(self._archive_candles_sync, target, older_than, timeframe)

    def _archive_candles_sync(self, target: Path, older_than: datetime, timeframe: str) -> int:
        self._pool.execute(
            f"COPY (SELECT * FROM candles WHERE ts < ? AND timeframe = ?) "
            f"TO '{target.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)",
            [older_than, timeframe],
        )
        rows = self._pool.execute(
            "SELECT count(*) FROM candles WHERE ts < ? AND timeframe = ?",
            [older_than, timeframe],
        )
        count = int(rows[0][0]) if rows else 0
        if count:
            self._pool.execute(
                "DELETE FROM candles WHERE ts < ? AND timeframe = ?", [older_than, timeframe]
            )
        return count

    async def compact_cold_archive(self) -> int:
        """Переупаковывает файлы архива: перезаписывает через Parquet со сжатием.

        Возвращает «освобождённые» байты: разницу между размером до и после.
        """
        return await asyncio.to_thread(self._compact_sync)

    def _compact_sync(self) -> int:
        freed = 0
        for path in sorted(self._archive_dir.glob("*.parquet")):
            before = path.stat().st_size
            tmp = path.with_suffix(".compact.parquet")
            self._pool.execute(
                f"COPY (SELECT * FROM read_parquet('{path.as_posix()}')) "
                f"TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            after = tmp.stat().st_size
            if after < before:
                tmp.replace(path)
                freed += before - after
            else:
                tmp.unlink(missing_ok=True)
        return freed

    async def usage_by_layer(self) -> dict[str, int]:
        """Занятое место по слоям в байтах."""
        return await asyncio.to_thread(self._usage_sync)

    def _usage_sync(self) -> dict[str, int]:
        db_file = Path(str(self._pool.db_path))
        hot = db_file.stat().st_size if db_file.exists() else 0
        warm = 0
        wal = db_file.with_name(db_file.name + ".wal")
        if wal.exists():
            warm = wal.stat().st_size
        cold = sum(p.stat().st_size for p in self._archive_dir.glob("*.parquet"))
        return {"hot": hot, "warm": warm, "cold": cold}

    async def total_usage_bytes(self) -> int:
        usage = await self.usage_by_layer()
        return sum(usage.values())

    async def export_backup(self, destination: Path) -> Path:
        """Экспорт: сжатая копия БД + архив в один каталог."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "redbot_backup.parquet"

        return await asyncio.to_thread(self._export_sync, target, BACKUP_TABLES)

    def _export_sync(self, target: Path, tables: tuple[str, ...]) -> Path:
        for table in tables:
            part = target.with_name(f"{target.stem}_{table}.parquet")
            self._pool.execute(
                f"COPY (SELECT * FROM {table}) TO '{part.as_posix()}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return target.parent

    async def restore_backup(self, source: Path) -> None:
        """Восстановление из Parquet-копий, созданных ``export_backup``."""
        source = Path(source)
        return await asyncio.to_thread(self._restore_sync, source)

    def _restore_sync(self, source: Path) -> None:
        paths = {table: source / f"redbot_backup_{table}.parquet" for table in BACKUP_TABLES}
        if any(not path.is_file() or path.is_symlink() for path in paths.values()):
            raise ValueError("Неполная или небезопасная резервная копия")
        # Валидация файлов ДО транзакции; удаляем/возвращаем все таблицы атомарно.
        for path in paths.values():
            escaped = path.as_posix().replace("'", "''")
            self._pool.execute(f"SELECT count(*) FROM read_parquet('{escaped}')")
        self._pool.restore_tables(paths)

    async def read_cold(self, pattern: str = "*.parquet") -> list[tuple[Any, ...]]:
        """Чтение холодных данных напрямую, без загрузки в горячий слой."""
        files = sorted(self._archive_dir.glob(pattern))
        if not files:
            return []
        listed = ", ".join(f"'{p.as_posix()}'" for p in files)
        return await asyncio.to_thread(
            self._pool.execute, f"SELECT count(*) FROM read_parquet([{listed}])"
        )
