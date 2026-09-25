"""Пульт управления: HTTP-формы работают независимо от WebSocket."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.gui_control import (
    get_control_state,
    restart_scheduler_task,
    set_soft_pause,
    start_bot,
    stop_bot,
)

router = APIRouter(tags=["control"], dependencies=[Depends(require_session)])


class ControlResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    mode: str
    uptime_seconds: int
    soft_paused: bool
    hard_stop_latched: bool
    tasks: list[dict[str, Any]]
    broker_adapter: str
    market_adapter: str


@router.get("/control")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/control.html",
        title="Пульт управления",
        section="control",
        data={"state": get_control_state(context)},
    )


@router.get("/api/control/state", response_model=ControlResponse)
async def state(context: ContextDep) -> ControlResponse:
    return ControlResponse.model_validate(get_control_state(context))


def _fragment(request: Request, context: Any, *, message: str = "", error: str = "") -> Any:
    return render_partial(
        request,
        "partials/control_state.html",
        {"state": get_control_state(context), "message": message, "error": error},
    )


@router.post("/control/pause")
async def pause(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    reason: str = Form(default="Ручная пауза через GUI"),
) -> Any:
    try:
        await set_soft_pause(context, engaged=True, reason=reason[:256])
    except ValueError as exc:
        return _fragment(request, context, error=str(exc))
    return _fragment(
        request, context, message="Новые входы заблокированы; монитор позиций активен."
    )


@router.post("/control/resume")
async def resume(request: Request, context: ContextDep, _session: SessionDep) -> Any:
    try:
        await set_soft_pause(context, engaged=False, reason="")
    except ValueError as exc:
        return _fragment(request, context, error=str(exc))
    return _fragment(request, context, message="Открытие новых планов разрешено.")


@router.post("/control/stop")
async def stop(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    confirmation: str = Form(default=""),
) -> Any:
    try:
        await stop_bot(context, confirmation=confirmation)
    except (ValueError, TimeoutError) as exc:
        return _fragment(request, context, error=str(exc))
    return _fragment(
        request, context, message="Торговый процесс остановлен. GUI остаётся доступен."
    )


@router.post("/control/start")
async def start(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    confirmation: str = Form(default=""),
) -> Any:
    try:
        await start_bot(context, confirmation=confirmation)
    except (ValueError, RuntimeError) as exc:
        return _fragment(request, context, error=str(exc))
    return _fragment(request, context, message="Торговый процесс запущен.")


@router.get("/control/tasks/{name}/logs")
async def task_logs(request: Request, context: ContextDep, name: str) -> Any:
    if context.scheduler is None or name not in {s.name for s in context.scheduler.tasks}:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    rows = request.app.state.logs.recent(module=name, limit=20)
    return render_partial(request, "partials/task_logs.html", {"rows": rows})


@router.get("/control/logs/export", response_class=PlainTextResponse)
async def export_logs(request: Request) -> PlainTextResponse:
    rows = request.app.state.logs.recent(limit=2000)
    text = "\n".join(f"{r['ts']} {r['level']} {r['module']}: {r['message']}" for r in rows)
    return PlainTextResponse(
        text,
        headers={
            "Content-Disposition": "attachment; filename=redbot-gui-logs.txt",
            "Cache-Control": "no-store",
        },
    )


@router.post("/control/tasks/{name}/restart")
async def restart_task(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    name: str,
) -> Any:
    try:
        restart_scheduler_task(context, name)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        return _fragment(request, context, error=str(exc))
    return _fragment(request, context, message=f"Повторный тик {name} запланирован.")
