"""Совместимый JSON API администрирования, только через application use cases."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.schemas import (
    ArchiveRequest,
    OkResponse,
    SqlConsoleRequest,
    SqlConsoleResponse,
    StorageResponse,
)
from application.use_cases.execute_readonly_query import execute_readonly_query
from application.use_cases.manage_storage import (
    archive_now,
    compact_archive,
    create_backup,
    storage_overview,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_session)])


@router.get("/storage", response_model=StorageResponse)
async def storage_usage(context: ContextDep) -> StorageResponse:
    try:
        usage, sizes = await storage_overview(context)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StorageResponse(usage_by_layer=usage, total_bytes=sum(usage.values()), table_sizes=sizes)


@router.post("/sql", response_model=SqlConsoleResponse)
async def sql_console(
    context: ContextDep,
    _session: SessionDep,
    payload: SqlConsoleRequest,
) -> SqlConsoleResponse:
    try:
        result = await execute_readonly_query(context, payload.query, row_limit=payload.row_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("sql_console_error", error=type(exc).__name__)
        raise HTTPException(status_code=400, detail="Ошибка выполнения SELECT") from exc
    return SqlConsoleResponse(columns=result.columns, rows=result.rows, truncated=result.truncated)


@router.post("/archive", response_model=OkResponse)
async def archive(
    context: ContextDep,
    _session: SessionDep,
    payload: ArchiveRequest,
) -> OkResponse:
    try:
        moved = await archive_now(context, days=payload.older_than_days)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OkResponse(ok=True, detail=f"Перенесено записей: {moved}")


@router.post("/compact", response_model=OkResponse)
async def compact(context: ContextDep, _session: SessionDep) -> OkResponse:
    try:
        freed = await compact_archive(context)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OkResponse(ok=True, detail=f"Освобождено байт: {freed}")


@router.post("/backup", response_model=OkResponse)
async def backup(context: ContextDep, _session: SessionDep) -> OkResponse:
    try:
        info = await create_backup(context)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return OkResponse(ok=True, detail=f"Бэкап {info.id} создан")
