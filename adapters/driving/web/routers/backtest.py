"""Backtest: защищённый экран. Запуск отключён до изолированного runner.

Грубая equity curve на данных LIVE недопустима: она выглядела бы как бэктест,
но могла бы посчитать вымышленные сделки. Вместо этого fail-closed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from adapters.driving.web.dependencies import ContextDep, require_session
from adapters.driving.web.render import render_page

router = APIRouter(tags=["backtest"], dependencies=[Depends(require_session)])


@router.get("/backtest")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/backtest.html",
        title="Backtest Runner",
        section="backtest",
        data={
            "instruments": context.tradable_instruments,
        },
    )


@router.post("/api/backtest/runs")
async def launch() -> None:
    raise HTTPException(status_code=501, detail="Изолированный backtest runner ещё не подключён")
