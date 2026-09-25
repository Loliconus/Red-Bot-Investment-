"""Системные ручки: статус, kill switch, health."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from adapters.driving.web.dependencies import ContextDep, SessionDep
from adapters.driving.web.schemas import (
    KillSwitchRequest,
    OkResponse,
    StatusResponse,
)

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/status", response_model=StatusResponse)
async def status(context: ContextDep) -> StatusResponse:
    plans = await context.repository.get_open_trade_plans()
    positions = await context.broker.get_open_positions()
    return StatusResponse(
        execution_mode=context.settings.execution_mode.value,
        started_at=context.started_at,
        kill_switch_engaged=bool(context.kill_switch and context.kill_switch.is_engaged),
        open_plans=len(plans),
        open_positions=len(positions),
        instruments=[i.ticker for i in context.instruments],
        config_version=context.config.version,
    )


@router.get("/health")
async def health() -> dict[str, Any]:
    """Публичная ручка без авторизации — для мониторинга и балансировщика."""
    return {"status": "ok", "timestamp": datetime.now(tz=UTC).isoformat()}


@router.post("/kill-switch", response_model=OkResponse)
async def kill_switch(
    context: ContextDep,
    _session: SessionDep,
    payload: KillSwitchRequest,
) -> OkResponse:
    if context.kill_switch is None:
        raise HTTPException(status_code=503, detail="Kill switch не инициализирован")
    if payload.engaged:
        await context.kill_switch.engage(payload.reason, initiated_by="gui")
    else:
        context.kill_switch.release()
    return OkResponse(
        ok=True, detail=f"kill switch: {'включён' if payload.engaged else 'выключен'}"
    )
