"""Администрирование БД через use cases: SQL, backup/restore, archive, retention."""

from __future__ import annotations

import csv
import io
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.jobs import JobManager
from adapters.driving.web.render import render_page, render_partial
from adapters.driving.web.security.session import get_session_manager
from application.use_cases.execute_readonly_query import QueryResult, execute_readonly_query
from application.use_cases.manage_storage import (
    archive_now,
    compact_archive,
    create_backup,
    list_backups,
    restore_backup,
    set_memory_limit,
    set_retention,
    storage_overview,
)

router = APIRouter(tags=["storage"], dependencies=[Depends(require_session)])


@router.get("/admin/storage")
async def page(request: Request, context: ContextDep, session: SessionDep) -> Any:
    usage, sizes = await storage_overview(context)
    manager = get_session_manager(request)
    state = manager.get(session)
    return render_page(
        request,
        "pages/storage.html",
        title="Администрирование БД",
        section="storage",
        data={
            "usage": usage,
            "sizes": sizes,
            "backups": await list_backups(context),
            "jobs": request.app.state.jobs.jobs,
            "stopped": not (context.scheduler and context.scheduler.is_active),
            "memory_limit_mb": context.storage_memory_limit_mb
            or context.settings.storage.duckdb_memory_limit_mb,
            "hot_days": int(
                await context.repository.get_operational_value("hot_retention_days")
                or context.settings.storage.hot_retention_days
            ),
            "warm_days": int(
                await context.repository.get_operational_value("warm_retention_days")
                or context.settings.storage.warm_retention_days
            ),
            "disk_threshold_pct": int(
                await context.repository.get_operational_value("disk_threshold_pct")
                or context.settings.storage.disk_usage_threshold_pct * 100
            ),
            "sql_history": state.sql_history if state else [],
        },
    )


@router.post("/admin/storage/sql")
async def sql_form(
    request: Request,
    context: ContextDep,
    session: SessionDep,
    query: str = Form(...),
) -> Any:
    try:
        result = await execute_readonly_query(context, query)
    except (ValueError, NotImplementedError) as exc:
        return render_partial(
            request, "partials/sql_result.html", {"error": str(exc)}, status_code=422
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Ошибка выполнения SELECT") from exc
    manager = get_session_manager(request)
    manager.remember_sql(session, query)
    export_id = uuid4().hex[:16]
    state = manager.get(session)
    if state:
        state.sql_exports = {export_id: result}  # ограничиваем память одним результатом
    return render_partial(
        request, "partials/sql_result.html", {"result": result, "export_id": export_id}
    )


@router.get("/admin/storage/sql/{export_id}.csv")
async def sql_csv(request: Request, session: SessionDep, export_id: str) -> Response:
    data = get_session_manager(request).get(session)
    result: QueryResult | None = data.sql_exports.get(export_id) if data else None
    if result is None:
        raise HTTPException(status_code=404, detail="Результат запроса истёк")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(result.columns)
    # Текстовые ячейки, начинающиеся с формул таблиц, экранируем.
    writer.writerows([_csv_safe(value) for value in row] for row in result.rows)
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=redbot-query.csv",
            "Cache-Control": "no-store",
        },
    )


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@router.post("/admin/storage/archive")
async def archive(request: Request, context: ContextDep, _session: SessionDep) -> Any:
    manager: JobManager = request.app.state.jobs

    async def work() -> str:
        moved = await archive_now(context)
        return f"Перенесено записей: {moved}"

    try:
        manager.start("archive", work)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return render_partial(request, "partials/storage_jobs.html", {"jobs": manager.jobs})


@router.post("/admin/storage/compact")
async def compact(request: Request, context: ContextDep, _session: SessionDep) -> Any:
    manager: JobManager = request.app.state.jobs

    async def work() -> str:
        freed = await compact_archive(context)
        return f"Освобождено байт: {freed}"

    try:
        manager.start("compact", work)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return render_partial(request, "partials/storage_jobs.html", {"jobs": manager.jobs})


@router.post("/admin/storage/backup")
async def backup(request: Request, context: ContextDep, _session: SessionDep) -> Any:
    manager: JobManager = request.app.state.jobs

    async def work() -> str:
        info = await create_backup(context)
        return f"Бэкап {info.id} создан ({info.size_bytes:,} байт)"

    try:
        manager.start("backup", work)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return render_partial(request, "partials/storage_jobs.html", {"jobs": manager.jobs})


@router.get("/admin/storage/jobs")
async def job_list(request: Request) -> Any:
    return render_partial(
        request, "partials/storage_jobs.html", {"jobs": request.app.state.jobs.jobs}
    )


@router.get("/admin/storage/backups")
async def backup_list(request: Request, context: ContextDep) -> Any:
    return render_partial(
        request,
        "partials/backup_list.html",
        {
            "backups": await list_backups(context),
            "stopped": not (context.scheduler and context.scheduler.is_active),
        },
    )


@router.post("/admin/storage/restore")
async def restore(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    backup_id: str = Form(...),
    confirmation: str = Form(default=""),
) -> Any:
    try:
        await restore_backup(context, backup_id, confirmation=confirmation)
    except (ValueError, PermissionError) as exc:
        return render_partial(
            request,
            "partials/backup_list.html",
            {
                "backups": await list_backups(context),
                "stopped": not (context.scheduler and context.scheduler.is_active),
                "error": str(exc),
            },
            status_code=409,
        )
    return render_partial(
        request,
        "partials/backup_list.html",
        {
            "backups": await list_backups(context),
            "stopped": True,
            "message": "Восстановлено. Для запуска требуется полный рестарт приложения.",
        },
    )


@router.post("/admin/storage/memory")
async def memory(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    limit_mb: int = Form(...),
    confirmed_warning: bool = Form(default=False),
) -> Any:
    try:
        await set_memory_limit(context, limit_mb, confirmed_warning=confirmed_warning)
    except ValueError as exc:
        return render_partial(
            request,
            "partials/storage_settings.html",
            {
                "error": str(exc),
                "memory_limit_mb": context.storage_memory_limit_mb
                or context.settings.storage.duckdb_memory_limit_mb,
            },
            status_code=422,
        )
    return render_partial(
        request,
        "partials/storage_settings.html",
        {"message": "Лимит DuckDB применён", "memory_limit_mb": limit_mb},
    )


@router.post("/admin/storage/retention")
async def retention(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    hot_days: int = Form(...),
    warm_days: int = Form(...),
    disk_threshold_pct: int = Form(...),
) -> Any:
    try:
        await set_retention(
            context, hot_days=hot_days, warm_days=warm_days, disk_threshold_pct=disk_threshold_pct
        )
    except ValueError as exc:
        return render_partial(
            request, "partials/retention_status.html", {"error": str(exc)}, status_code=422
        )
    return render_partial(
        request,
        "partials/retention_status.html",
        {"message": "Политика вступит в силу при следующем цикле архивации."},
    )
