"""Порт архивации: перенос данных между слоями хранения."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArchivePort(Protocol):
    """Перенос hot → warm → cold и контроль занятого места."""

    async def archive_snapshots(self, older_than: datetime) -> int:
        """Переносит снапшоты старше указанной даты в холодный слой.

        Возвращает количество перенесённых записей.
        """
        ...

    async def compact_cold_archive(self) -> int:
        """Пересжимает холодный архив. Возвращает освобождённые байты."""
        ...

    async def usage_by_layer(self) -> dict[str, int]:
        """Занятое место по слоям в байтах: ``{"hot": ..., "warm": ..., "cold": ...}``."""
        ...

    async def total_usage_bytes(self) -> int: ...

    async def export_backup(self, destination: Path) -> Path:
        """Экспорт снимка БД в архив."""
        ...

    async def restore_backup(self, source: Path) -> None: ...
