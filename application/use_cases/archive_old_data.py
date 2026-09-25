"""Архивация: перенос горячих данных в холодный слой.

Порог занятого места задаётся в конфиге (``storage.disk_usage_threshold_pct``):
как только занято больше порога, старые снапшоты уезжают в Parquet-архив
(снапшоты — самые объёмные данные, свечи компактнее).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveReport:
    archived_rows: int
    freed_bytes: int
    usage_by_layer: dict[str, int]
    threshold_exceeded: bool


async def archive_old_data(ctx: AppContext) -> ArchiveReport:
    """Переносит устаревшие снапшоты в холодный слой и уплотняет архив."""
    if ctx.archive is None:
        return ArchiveReport(
            archived_rows=0,
            freed_bytes=0,
            usage_by_layer={},
            threshold_exceeded=False,
        )

    now = ctx.clock.now()
    retention = int(
        await ctx.repository.get_operational_value("warm_retention_days")
        or ctx.settings.storage.warm_retention_days
    )
    cutoff = now - timedelta(days=retention)

    archived = await ctx.archive.archive_snapshots(older_than=cutoff)
    freed = await ctx.archive.compact_cold_archive()
    usage = await ctx.archive.usage_by_layer()

    total = sum(usage.values())
    threshold_exceeded = _is_threshold_exceeded(ctx, total, usage)

    if threshold_exceeded:
        logger.warning(
            "storage_threshold_exceeded",
            total_bytes=total,
            threshold_pct=ctx.settings.storage.disk_usage_threshold_pct,
        )
        if ctx.notifier is not None:
            await ctx.notifier.send(
                f"Хранилище занято: {total / 1024**3:.2f} ГБ. "
                "Рекомендуется увеличить архивацию или освободить место."
            )

    logger.info("archive_completed", archived_rows=archived, freed_bytes=freed)
    return ArchiveReport(
        archived_rows=archived,
        freed_bytes=freed,
        usage_by_layer=usage,
        threshold_exceeded=threshold_exceeded,
    )


def _is_threshold_exceeded(ctx: AppContext, total_bytes: int, usage: dict[str, int]) -> bool:
    """Проверяет превышение порога.

    Порог задан в процентах, но абсолютного размера диска мы не знаем,
    поэтому сравниваем долю горячего слоя в общем объёме данных: если горячий
    слой раздулся — пора архивировать агрессивнее.
    """
    if total_bytes == 0:
        return False
    hot = usage.get("hot", 0)
    return (hot / total_bytes) > ctx.settings.storage.disk_usage_threshold_pct
