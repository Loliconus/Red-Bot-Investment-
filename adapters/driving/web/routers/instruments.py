"""Инструменты: управление торговой корзиной, каталог Мосбиржи и параметры ТА."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.manage_instruments import (
    add_instrument,
    list_instrument_views,
    remove_instrument,
    set_instrument_enabled,
)
from config.catalog import get_catalog_instruments

router = APIRouter(tags=["instruments"], dependencies=[Depends(require_session)])


class InstrumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=64)
    class_code: str = Field(default="TQBR", min_length=1, max_length=15)


@router.get("/instruments")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/instruments.html",
        title="Инструменты и ТА",
        section="instruments",
        data={
            "instruments": await list_instrument_views(context),
            "catalog": get_catalog_instruments(),
        },
    )


@router.get("/api/instruments")
async def list_api(context: ContextDep) -> list[dict[str, object]]:
    return await list_instrument_views(context)


@router.get("/api/instruments/catalog")
async def catalog_api() -> list[dict[str, Any]]:
    return get_catalog_instruments()


@router.post("/api/instruments")
async def create_api(
    context: ContextDep, _session: SessionDep, payload: InstrumentCreate
) -> dict[str, str]:
    try:
        instrument = await add_instrument(context, payload.ticker, payload.class_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        instrument = await add_instrument(context, ticker, class_code)
        message, error = f"Инструмент {instrument.ticker} успешно добавлен в корзину", ""
    except Exception as exc:  # noqa: BLE001
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


@router.post("/instruments/{uid}/delete")
async def delete_item(request: Request, context: ContextDep, _session: SessionDep, uid: str) -> Any:
    try:
        await remove_instrument(context, uid)
        message, error = "Инструмент удалён из корзины", ""
    except Exception as exc:  # noqa: BLE001
        message, error = "", str(exc)
    return render_partial(
        request,
        "partials/instrument_table.html",
        {"instruments": await list_instrument_views(context), "message": message, "error": error},
    )
