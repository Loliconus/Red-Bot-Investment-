"""Kill-switch, аудит и write-only токен; задержка подтверждения проверяется сервером."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from adapters.driving.web.security.session import get_session_manager
from application.use_cases.gui_control import get_control_state, stop_bot
from application.use_cases.manage_security import read_audit, save_broker_token, token_metadata

router = APIRouter(tags=["security"], dependencies=[Depends(require_session)])


class AuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    ts: datetime
    section: str
    action: str
    before: dict[str, str]
    after: dict[str, str]
    outcome: str


async def _entries(
    context: Any,
    section: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    records = await read_audit(context, section=section, action=action, since=since)
    return [
        {
            "id": str(e.id),
            "ts": e.ts.isoformat(),
            "section": e.section,
            "action": e.action,
            "before": e.before,
            "after": e.after,
            "outcome": e.outcome,
        }
        for e in records
    ]


@router.get("/security")
async def page(request: Request, context: ContextDep, section: str | None = None) -> Any:
    return render_page(
        request,
        "pages/security.html",
        title="Безопасность",
        section="security",
        data={
            "entries": await _entries(context, section=section),
            "filter_section": section or "",
            "state": get_control_state(context),
            "token_meta": await token_metadata(context),
        },
    )


@router.get("/api/security/audit", response_model=list[AuditResponse])
async def audit_api(
    context: ContextDep,
    section: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditResponse]:
    return [
        AuditResponse.model_validate(
            {
                "id": str(e.id),
                "ts": e.ts,
                "section": e.section,
                "action": e.action,
                "before": e.before,
                "after": e.after,
                "outcome": e.outcome,
            }
        )
        for e in await read_audit(context, section=section, action=action, since=since, limit=limit)
    ]


@router.post("/security/kill/challenge")
async def challenge(request: Request, _session: SessionDep) -> Any:
    nonce = get_session_manager(request).issue_kill_challenge(_session)
    return render_partial(request, "partials/kill_confirm.html", {"nonce": nonce})


@router.post("/security/kill/confirm")
async def kill_confirm(
    request: Request,
    context: ContextDep,
    session: SessionDep,
    nonce: str = Form(...),
    confirmation: str = Form(default=""),
) -> Any:
    try:
        if confirmation != "ОСТАНОВИТЬ":
            raise ValueError("Введите точно ОСТАНОВИТЬ")
        get_session_manager(request).consume_kill_challenge(session, nonce)
        await stop_bot(context, confirmation=confirmation, hard_stop=True)
    except (ValueError, TimeoutError) as exc:
        return render_partial(
            request,
            "partials/kill_confirm.html",
            {"nonce": nonce, "error": str(exc)},
            status_code=422,
        )
    return render_partial(
        request,
        "partials/kill_confirm.html",
        {"stopped": True, "message": "Бот остановлен. Открытые позиции требуют контроля."},
    )


@router.post("/security/token")
async def save_token(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    api_token: str = Form(...),
) -> Any:
    request.state.audit_after = {"api_token": "[сохранён в keyring]"}
    try:
        updated = await save_broker_token(context, api_token)
    except ValueError as exc:
        return render_partial(
            request,
            "partials/token_status.html",
            {"error": str(exc), "token_meta": await token_metadata(context)},
            status_code=422,
        )
    except Exception as exc:
        # Исключение keyring не должно содержать значение секрета в HTTP/логах.
        raise HTTPException(
            status_code=503, detail="Системное хранилище ключей недоступно"
        ) from exc
    request.app.state.logs.redact_secret(api_token)
    return render_partial(
        request,
        "partials/token_status.html",
        {
            "message": "Сохранено. Требуется рестарт приложения.",
            "token_meta": {"source": "keyring", "updated_at": updated},
        },
        hx_trigger="token-saved",
    )
