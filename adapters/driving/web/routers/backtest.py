"""Синтетический трейдер: защищённый GUI / отдельный worker без broker доступа."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from adapters.driving.web.dependencies import ContextDep, require_session
from adapters.driving.web.render import render_page
from application.use_cases.research import ResearchService, evaluate_final, launch_research
from synthetic_trader.config import ExperimentConfig

router = APIRouter(tags=["synthetic-trader"], dependencies=[Depends(require_session)])


class FinalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


def service(context: Any) -> ResearchService:
    if context.research is None:
        raise HTTPException(status_code=503, detail="Research service не инициализирован")
    result: ResearchService = context.research
    return result


@router.get("/backtest")
@router.get("/synthetic")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/backtest.html",
        title="Синтетический трейдер",
        section="backtest",
        data={"defaults": ExperimentConfig().model_dump(mode="json"), "research_only": True},
    )


@router.get("/api/backtest/runs")
async def runs(context: ContextDep) -> dict[str, Any]:
    return {"runs": await asyncio.to_thread(service(context).runs), "live_enabled": False}


@router.post("/api/backtest/runs", status_code=202)
async def launch(payload: ExperimentConfig, context: ContextDep) -> dict[str, Any]:
    try:
        return await launch_research(service(context), payload.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/backtest/runs/{run_id}")
async def run(run_id: str, context: ContextDep, report: bool = True) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(service(context).get, run_id, report=report)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/backtest/runs/{run_id}/cancel")
async def cancel(run_id: str, context: ContextDep) -> dict[str, Any]:
    try:
        return await service(context).cancel(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/backtest/runs/{run_id}/final", status_code=202)
async def final(run_id: str, payload: FinalRequest, context: ContextDep) -> dict[str, Any]:
    try:
        return await evaluate_final(service(context), run_id, payload.confirmation)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/backtest/runs/{run_id}/artifacts/{name}")
async def artifact(run_id: str, name: str, context: ContextDep) -> FileResponse:
    try:
        path = service(context).artifact(run_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=f"{run_id[:8]}-{name}")
