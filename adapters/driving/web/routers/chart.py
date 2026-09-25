"""Лёгкий SSR-каркас и данные для локальной копии Lightweight Charts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from adapters.driving.web.dependencies import ContextDep, require_session
from adapters.driving.web.render import render_page
from application.use_cases.gui_views import chart_view
from core.domain.enums import Timeframe

router = APIRouter(tags=["chart"], dependencies=[Depends(require_session)])


class ChartResponse(BaseModel):
    uid: str
    ticker: str
    timeframe: str
    bars: list[dict[str, Any]]
    reasoning: dict[str, Any] | None
    bids: list[dict[str, Any]]
    asks: list[dict[str, Any]]
    fibonacci: dict[str, str]
    benchmark: list[dict[str, Any]]
    markers: list[dict[str, Any]]


@router.get("/chart/{uid}")
async def page(request: Request, context: ContextDep, uid: str) -> Any:
    instrument = next((i for i in context.instruments if i.uid == uid), None)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Инструмент не найден")
    return render_page(
        request,
        "pages/chart.html",
        title=f"График · {instrument.ticker}",
        section="chart",
        data={"instrument": instrument},
    )


@router.get("/api/chart/{uid}", response_model=ChartResponse)
async def data(context: ContextDep, uid: str, timeframe: Timeframe = Timeframe.H1) -> ChartResponse:
    try:
        return ChartResponse.model_validate(await chart_view(context, uid, timeframe))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
