"""Инструменты: управление торговой корзиной, каталог API и параметры ТА.

Каталог не хранится в коде: он загружается из ``InstrumentsService`` и
сохраняется в БД, поэтому все эндпоинты читают его из хранилища, а обновление
выполняется явным действием (или автоматически при устаревании).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.manage_instrument_catalog import (
    DEFAULT_CATALOG_LIMIT,
    catalog_status,
    catalog_view,
    list_catalog_views,
    refresh_instrument_catalog,
    search_instruments,
)
from application.use_cases.manage_instruments import (
    add_instrument,
    list_instrument_views,
    remove_instrument,
    set_instrument_enabled,
)

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
            "catalog": await list_catalog_views(context, limit=DEFAULT_CATALOG_LIMIT),
            "catalog_status": await catalog_status(context),
        },
    )


@router.get("/api/instruments")
async def list_api(context: ContextDep) -> list[dict[str, object]]:
    return await list_instrument_views(context)


@router.get("/api/instruments/catalog")
async def catalog_api(
    context: ContextDep,
    query: str | None = Query(default=None, max_length=64),
    instrument_type: str | None = Query(default=None, max_length=15),
    tradable: bool = Query(default=True),
    limit: int = Query(default=DEFAULT_CATALOG_LIMIT, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Каталог из сохранённых данных: поиск по тикеру, названию и ISIN."""
    return await list_catalog_views(
        context,
        query=query,
        instrument_types=[instrument_type] if instrument_type else None,
        tradable_only=tradable,
        limit=limit,
    )


@router.get("/api/instruments/search")
async def search_api(
    context: ContextDep,
    query: str = Query(min_length=2, max_length=64),
    instrument_type: str | None = Query(default=None, max_length=15),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Поиск инструмента: сохранённый каталог + дозагрузка из FindInstrument."""
    entries = await search_instruments(context, query, instrument_type=instrument_type, limit=limit)
    return [catalog_view(entry) for entry in entries]


@router.post("/api/instruments/catalog/refresh")
async def catalog_refresh_api(
    context: ContextDep,
    _session: SessionDep,
    instrument_type: list[str] | None = Query(default=None),
) -> dict[str, Any]:
    """Обновляет каталог из API и сохраняет его в БД."""
    types = [item.strip().lower() for item in (instrument_type or []) if item.strip()]
    try:
        result = await refresh_instrument_catalog(context, instrument_types=types or None)
    except Exception as exc:  # исключение наружу как 502 — текст уходит в GUI
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "types": list(result.types),
        "fetched": result.fetched,
        "updated_at": result.updated_at.isoformat(),
    }


@router.post("/instruments/catalog/refresh")
async def catalog_refresh_form(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    instrument_type: list[str] = Form(default=[]),
) -> Any:
    """HTMX-обновление каталога: перерисовывает панель инструментов."""
    types = [item.strip().lower() for item in instrument_type if item.strip()]
    try:
        result = await refresh_instrument_catalog(context, instrument_types=types or None)
        message = (
            f"Каталог обновлён из API: {result.fetched} инструментов ({', '.join(result.types)})"
        )
        error = ""
    except Exception as exc:  # noqa: BLE001
        message, error = "", str(exc)
    return render_partial(
        request,
        "partials/instrument_catalog.html",
        {
            "catalog": await list_catalog_views(context, limit=DEFAULT_CATALOG_LIMIT),
            "catalog_status": await catalog_status(context),
            "message": message,
            "error": error,
        },
    )


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
