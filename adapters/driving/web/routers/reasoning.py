"""Мысли бота: воронка решений, причины молчания, покрытие корзины."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from adapters.driving.web.dependencies import ContextDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.decision_diagnostics import reasoning_overview

router = APIRouter(tags=["reasoning"], dependencies=[Depends(require_session)])


@router.get("/reasoning")
async def page(request: Request, context: ContextDep, hours: int = 24) -> Any:
    return render_page(
        request,
        "pages/reasoning.html",
        title="Мысли бота",
        section="reasoning",
        data={"reasoning": await reasoning_overview(context, hours=hours)},
    )


@router.get("/reasoning/live")
async def live_fragment(request: Request, context: ContextDep, hours: int = 24) -> Any:
    """HTMX-фрагмент: перерисовка сводки после завершения цикла (WS-триггер)."""
    return render_partial(
        request,
        "partials/reasoning_live.html",
        {"reasoning": await reasoning_overview(context, hours=hours)},
    )


@router.get("/api/reasoning")
async def data(context: ContextDep, hours: int = 24) -> dict[str, Any]:
    return await reasoning_overview(context, hours=hours)
