"""Cockpit: приоритетные события, планы, журнал решений и портфель."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from adapters.driving.web.dependencies import ContextDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.gui_control import get_control_state
from application.use_cases.gui_views import dashboard_view, decision_detail

router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_session)])


class DashboardResponse(BaseModel):
    priority: list[dict[str, Any]]
    plans: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    portfolio: dict[str, Any]
    benchmark: str


@router.get("/")
async def page(request: Request, context: ContextDep, period: str = "session") -> Any:
    return render_page(
        request,
        "pages/dashboard.html",
        title="Обзор",
        section="dashboard",
        data={
            "dashboard": await dashboard_view(context, period=period),
            "state": get_control_state(context),
        },
    )


@router.get("/api/dashboard", response_model=DashboardResponse)
async def data(context: ContextDep, period: str = "session") -> DashboardResponse:
    return DashboardResponse.model_validate(await dashboard_view(context, period=period))


@router.get("/dashboard/decision/{decision_id}")
async def decision(request: Request, context: ContextDep, decision_id: UUID) -> Any:
    detail = await decision_detail(context, decision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return render_partial(request, "partials/decision_detail.html", {"decision": detail})
