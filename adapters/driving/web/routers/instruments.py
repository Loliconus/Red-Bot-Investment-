"""Инструменты: soft-toggle и добавление через use case с лимитом 2–5."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.manage_instruments import (
    add_instrument,
    list_instrument_views,
    set_instrument_enabled,
)

router = APIRouter(tags=["instruments"], dependencies=[Depends(require_session)])


class InstrumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=15)
    class_code: str = Field(default="TQBR", min_length=1, max_length=15)


@router.get("/instruments")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/instruments.html",
        title="Инструменты и ТА",
        section="instruments",
        data={"instruments": await list_instrument_views(context)},
    )


@router.get("/api/instruments")
async def list_api(context: ContextDep) -> list[dict[str, object]]:
    return await list_instrument_views(context)


@router.post("/api/instruments")
async def create_api(
    context: ContextDep, _session: SessionDep, payload: InstrumentCreate
) -> dict[str, str]:
    try:
        instrument = await add_instrument(context, payload.ticker, payload.class_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"uid": instrument.uid, "ticker": instrument.ticker}


@router.post("/instruments/add")
async def add_form(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    ticker: str = Form(...),
    class_code: str = Form(default="TQBR"),
) -> Any:
    try:
        await add_instrument(context, ticker, class_code)
        message, error = "Инструмент добавлен", ""
    except ValueError as exc:
        message, error = "", str(exc)
    return render_partial(
        request,
        "partials/instrument_table.html",
        {"instruments": await list_instrument_views(context), "message": message, "error": error},
    )


@router.post("/instruments/{uid}/toggle")
async def toggle(
    request: Request, context: ContextDep, _session: SessionDep, uid: str, enabled: str = Form(...)
) -> Any:
    try:
        await set_instrument_enabled(context, uid, enabled=enabled == "true")
        error = ""
    except ValueError as exc:
        error = str(exc)
    return render_partial(
        request,
        "partials/instrument_table.html",
        {"instruments": await list_instrument_views(context), "error": error},
    )
