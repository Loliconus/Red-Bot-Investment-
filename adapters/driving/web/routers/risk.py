"""Риск-панель: hard stop без DOM-переключателя, счёт меняется в 3 шага."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from adapters.driving.web.security.session import get_session_manager
from application.use_cases.manage_risk import (
    COUNTERTREND_PHRASE,
    change_managed_account,
    get_risk_state,
    mask_account,
    update_risk,
    validate_account_change,
)

router = APIRouter(tags=["risk"], dependencies=[Depends(require_session)])
CONFIRM_ACCOUNT = "СМЕНИТЬ СЧЁТ"


class RiskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    multiplier: Decimal | None = Field(default=None, ge=1, le=5)  # шаг 0.1 в update_risk
    risk_per_trade_pct: Decimal | None = Field(default=None, gt=0, le=Decimal("0.05"))
    allow_counter_trend: bool | None = None
    max_holding_hours: int | None = Field(default=None, ge=1, le=720)
    confirmed_warning: bool = False
    confirmation: str = ""


class RiskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    masked_account_id: str
    risk_per_trade_pct: Decimal
    max_loss_rub: Decimal | None
    multiplier: Decimal
    required_target_pct: Decimal
    allow_counter_trend: bool
    balance_rub: Decimal | None
    config_version: int


@router.get("/risk")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/risk.html",
        title="Риск-модуль",
        section="risk",
        data={"risk": get_risk_state(context)},
    )


@router.get("/api/risk", response_model=RiskResponse)
async def state(context: ContextDep) -> RiskResponse:
    return RiskResponse.model_validate(get_risk_state(context))


async def _update(context: Any, payload: RiskUpdate) -> RiskResponse:
    if (
        payload.allow_counter_trend
        and not context.config.allow_counter_trend
        and (not payload.confirmed_warning or payload.confirmation != COUNTERTREND_PHRASE)
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Для включения контр-тренда введите точно {COUNTERTREND_PHRASE}",
        )
    state = await update_risk(
        context,
        multiplier=payload.multiplier,
        risk_per_trade_pct=payload.risk_per_trade_pct,
        allow_counter_trend=payload.allow_counter_trend,
        max_holding_hours=payload.max_holding_hours,
    )
    return RiskResponse.model_validate(state)


@router.put("/api/risk", response_model=RiskResponse)
async def update_api(
    context: ContextDep, _session: SessionDep, payload: RiskUpdate
) -> RiskResponse:
    try:
        return await _update(context, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/risk/settings")
async def settings_form(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    multiplier: str = Form(...),
    risk_percent: str = Form(...),
    max_holding_hours: int = Form(...),
    allow_counter_trend: str = Form(default="false"),
    confirmed_warning: str = Form(default="false"),
    confirmation: str = Form(default=""),
) -> Any:
    try:
        payload = RiskUpdate.model_validate(
            {
                "multiplier": multiplier,
                "risk_per_trade_pct": Decimal(risk_percent) / Decimal("100"),
                "max_holding_hours": max_holding_hours,
                "allow_counter_trend": allow_counter_trend == "true",
                "confirmed_warning": confirmed_warning == "true",
                "confirmation": confirmation,
            }
        )
        await _update(context, payload)
        return render_partial(
            request,
            "partials/risk_settings.html",
            {"risk": get_risk_state(context), "message": "Параметры применены без рестарта."},
            hx_trigger=json.dumps(
                {
                    "risk-updated": {
                        "required_target_pct": str(get_risk_state(context).required_target_pct),
                    }
                }
            ),
        )
    except (ValueError, InvalidOperation, HTTPException) as exc:
        return render_partial(
            request,
            "partials/risk_settings.html",
            {"risk": get_risk_state(context), "error": str(getattr(exc, "detail", exc))},
            status_code=422,
        )


@router.post("/risk/account/reveal")
async def reveal_account(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    password: str = Form(...),
) -> HTMLResponse:
    if not get_session_manager(request).check_password(password):
        raise HTTPException(status_code=403, detail="Неверный пароль GUI")
    return render_partial(
        request, "partials/account_value.html", {"account_id": context.active_account_id}
    )


@router.post("/risk/account/step/1")
async def account_step1(request: Request, context: ContextDep, _session: SessionDep) -> Any:
    get_session_manager(request).begin_account_change(_session)
    request.state.audit_after = {"account_step": "1 просмотр текущего"}
    return render_partial(
        request,
        "partials/account_wizard.html",
        {"step": 2, "masked": mask_account(context.active_account_id)},
    )


@router.post("/risk/account/step/2")
async def account_step2(
    request: Request,
    context: ContextDep,
    session: SessionDep,
    new_account_id: str = Form(...),
) -> Any:
    request.state.audit_after = {
        "account_step": "2 ввод нового ID",
        "account_candidate": new_account_id,
    }
    try:
        validate_account_change(context, new_account_id)
        get_session_manager(request).draft_account(session, new_account_id)
    except (ValueError, PermissionError) as exc:
        return render_partial(
            request,
            "partials/account_wizard.html",
            {"step": 2, "masked": mask_account(context.active_account_id), "error": str(exc)},
            status_code=422,
        )
    return render_partial(
        request,
        "partials/account_wizard.html",
        {
            "step": 3,
            "masked": mask_account(context.active_account_id),
            "candidate": mask_account(new_account_id),
        },
    )


@router.post("/risk/account/step/3")
async def account_step3(
    request: Request,
    context: ContextDep,
    session: SessionDep,
    confirmation: str = Form(default=""),
) -> Any:
    request.state.audit_after = {"account_step": "3 подтверждение"}
    if confirmation != CONFIRM_ACCOUNT:
        return render_partial(
            request,
            "partials/account_wizard.html",
            {"step": 3, "error": f"Введите точно: {CONFIRM_ACCOUNT}"},
            status_code=422,
        )
    try:
        candidate = get_session_manager(request).take_account_draft(session)
        await change_managed_account(context, candidate)
    except (ValueError, PermissionError) as exc:
        return render_partial(
            request, "partials/account_wizard.html", {"step": 1, "error": str(exc)}, status_code=409
        )
    request.state.audit_after = {"account_step": "3 завершено", "account_candidate": candidate}
    return render_partial(
        request, "partials/account_wizard.html", {"step": 4, "candidate": mask_account(candidate)}
    )
