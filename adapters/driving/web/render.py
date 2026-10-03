"""Контекст Jinja2 layout (Mode Stripe и навигация на каждой странице)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from adapters.driving.web.navigation import NAVIGATION
from adapters.driving.web.security.session import COOKIE_NAME, SessionManager
from application.composition import AppContext

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def render_page(
    request: Request,
    template: str,
    *,
    title: str,
    section: str,
    data: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context: AppContext = request.app.state.context
    manager: SessionManager = request.app.state.sessions
    token = request.cookies.get(COOKIE_NAME, "")
    session = manager.get(token)
    chart_uid = next((i.uid for i in context.instruments if not i.is_benchmark), "")
    resolved = [
        {
            "key": item.key,
            "label": item.label,
            "path": f"/chart/{chart_uid}"
            if item.key == "chart" and chart_uid
            else (item.path if item.key != "chart" else "/instruments"),
            "icon": item.icon,
            "group": item.group,
            "hint": item.hint,
        }
        for item in NAVIGATION
    ]
    navigation: list[dict[str, Any]] = []
    for item in resolved:
        if not navigation or navigation[-1]["group"] != item["group"]:
            navigation.append({"group": item["group"], "items": []})
        navigation[-1]["items"].append(item)
    variables: dict[str, Any] = {
        "request": request,
        "title": title,
        "section": section,
        "nav": navigation,
        "mode": context.execution_mode.value.upper(),
        "csrf_token": manager.csrf(token) if session is not None else "",
        "authenticated": session is not None,
        "run_id": None,
        "actions": session.actions if session is not None else [],
        "restart_required": context.restart_required or context.hard_stop_latched,
        "active_instruments": [i for i in context.instruments if not i.is_benchmark],
    }
    variables.update(data or {})
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context=variables,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def render_partial(
    request: Request,
    template: str,
    data: dict[str, Any],
    *,
    status_code: int = 200,
    hx_trigger: str | None = None,
) -> HTMLResponse:
    headers = {"Cache-Control": "no-store"}
    if hx_trigger:
        headers["HX-Trigger"] = hx_trigger
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={"request": request, **data},
        status_code=status_code,
        headers=headers,
    )
