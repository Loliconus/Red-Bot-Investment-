"""Раздел администрирования БД.

Главная опасность такого раздела — дать пользователю произвольный SQL. Поэтому:
* read-only консоль проверяется ``is_select_only`` (только SELECT/WITH/PRAGMA
  и ни одной мутирующей конструкции, без точек с запятой);
* результат жёстко обрезается по ``row_limit``;
* все остальные операции (архивация, экспорт) — отдельные явные ручки,
  а не «выполнить любой запрос».
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from adapters.driven.storage.connection_pool import is_select_only
from adapters.driving.web.dependencies import (
    ContextDep,
    SessionDep,
    require_session,
)
from adapters.driving.web.schemas import (
    ArchiveRequest,
    OkResponse,
    SqlConsoleRequest,
    SqlConsoleResponse,
    StorageResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_session)],
)


@router.get("/storage", response_model=StorageResponse)
async def storage_usage(context: ContextDep) -> StorageResponse:
    if context.archive is None:
        raise HTTPException(status_code=503, detail="Архив не настроен")
    usage = await context.archive.usage_by_layer()
    sizes = await context.repository.table_sizes()
    return StorageResponse(
        usage_by_layer=usage,
        total_bytes=sum(usage.values()),
        table_sizes=sizes,
    )


@router.post("/sql", response_model=SqlConsoleResponse)
async def sql_console(
    context: ContextDep,
    _session: SessionDep,
    payload: SqlConsoleRequest,
) -> SqlConsoleResponse:
    """Read-only SQL-консоль."""
    if not is_select_only(payload.query):
        raise HTTPException(
            status_code=400,
            detail="Разрешены только read-only запросы (SELECT/WITH/PRAGMA/DESCRIBE/EXPLAIN)",
        )

    try:
        rows = await context.repository.execute_readonly(payload.query)
    except NotImplementedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("sql_console_error", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    truncated = len(rows) > payload.row_limit
    limited = rows[: payload.row_limit]
    columns = [f"col_{i}" for i in range(len(limited[0]))] if limited else []
    return SqlConsoleResponse(
        columns=columns,
        rows=[[_stringify(value) for value in row] for row in limited],
        truncated=truncated,
    )


def _stringify(value: Any) -> Any:
    """Приводит Decimal/datetime к JSON-совместимому виду."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return float(value) if isinstance(value, int) is False and _is_number(value) else value


def _is_number(value: Any) -> bool:
    from decimal import Decimal

    return isinstance(value, Decimal)


@router.post("/archive", response_model=OkResponse)
async def archive(
    context: ContextDep,
    _session: SessionDep,
    payload: ArchiveRequest,
) -> OkResponse:
    """Переносит данные старше указанного срока в холодный архив."""
    if context.archive is None:
        raise HTTPException(status_code=503, detail="Архив не настроен")
    cutoff = context.clock.now() - timedelta(days=payload.older_than_days)
    moved = await context.archive.archive_snapshots(older_than=cutoff)
    return OkResponse(ok=True, detail=f"Перенесено записей: {moved}")


@router.post("/compact", response_model=OkResponse)
async def compact(context: ContextDep, _session: SessionDep) -> OkResponse:
    if context.archive is None:
        raise HTTPException(status_code=503, detail="Архив не настроен")
    freed = await context.archive.compact_cold_archive()
    return OkResponse(ok=True, detail=f"Освобождено байт: {freed}")


@router.post("/backup", response_model=OkResponse)
async def backup(context: ContextDep, _session: SessionDep) -> OkResponse:
    from pathlib import Path

    if context.archive is None:
        raise HTTPException(status_code=503, detail="Архив не настроен")
    destination = Path(context.settings.storage.data_dir) / "backups"
    target = await context.archive.export_backup(destination)
    return OkResponse(ok=True, detail=f"Бэкап создан: {target}")
