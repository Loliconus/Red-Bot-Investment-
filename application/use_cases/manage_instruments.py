"""Управление корзиной через MarketDataPort и RepositoryPort."""

from __future__ import annotations

from application.composition import AppContext
from config.catalog import extract_ticker, get_catalog_name
from core.domain.entities import Instrument

MAX_TRADABLE = 5
MIN_TRADABLE = 2


async def list_instrument_views(context: AppContext) -> list[dict[str, object]]:
    return [
        {
            "uid": i.uid,
            "ticker": i.ticker,
            "name": get_catalog_name(i.ticker),
            "class_code": i.class_code,
            "lot_size": i.lot_size,
            "benchmark": i.is_benchmark,
            "enabled": context.instrument_enabled.get(i.uid, True),
        }
        for i in context.instruments
    ]


async def add_instrument(context: AppContext, ticker: str, class_code: str = "TQBR") -> Instrument:
    raw_ticker = ticker
    ticker = extract_ticker(ticker).strip().upper()
    class_code = class_code.strip().upper() or "TQBR"
    if not ticker or len(ticker) > 15 or len(class_code) > 15:
        msg = f"Укажите корректный тикер (введено: '{raw_ticker}') и код класса"
        raise ValueError(msg)
    if len(context.tradable_instruments) >= MAX_TRADABLE:
        msg = f"Допустимо не более {MAX_TRADABLE} активных торгуемых инструментов. Отключите один из активных инструментов в таблице."
        raise ValueError(msg)
    if any(i.ticker == ticker for i in context.instruments):
        msg = f"Инструмент {ticker} уже добавлен в корзину"
        raise ValueError(msg)
    if ticker == "IMOEX":
        raise ValueError("IMOEX — справочный бенчмарк, не торгуется")

    instrument = await context.market_data.resolve_instrument(ticker, class_code)
    if any(i.uid == instrument.uid for i in context.instruments):
        msg = f"Инструмент {ticker} (UID: {instrument.uid}) уже добавлен в корзину"
        raise ValueError(msg)
    if instrument.is_benchmark:
        raise ValueError("Бенчмарк не торгуется в рабочей корзине")

    await context.repository.save_instrument(instrument)
    context.instruments.append(instrument)
    context.instrument_enabled[instrument.uid] = True
    return instrument


async def remove_instrument(context: AppContext, uid: str) -> None:
    """Удаляет инструмент из корзины и репозитория."""
    instrument = next((i for i in context.instruments if i.uid == uid), None)
    if instrument is None:
        raise ValueError("Инструмент не найден")
    if instrument.is_benchmark or instrument.ticker == "IMOEX":
        raise ValueError("IMOEX — справочный бенчмарк, его нельзя удалить")

    # Удаляем из репозитория
    await context.repository.delete_instrument(uid)
    await context.repository.set_operational_value(f"instrument:{uid}:enabled", "")

    # Удаляем из памяти контекста
    context.instruments = [i for i in context.instruments if i.uid != uid]
    context.instrument_enabled.pop(uid, None)


async def set_instrument_enabled(context: AppContext, uid: str, *, enabled: bool) -> None:
    instrument = next((i for i in context.instruments if i.uid == uid), None)
    if instrument is None or instrument.is_benchmark:
        raise ValueError("Инструмент не найден или является бенчмарком")
    if (
        enabled
        and not context.instrument_enabled.get(uid, True)
        and len(context.tradable_instruments) >= 5
    ):
        raise ValueError("Допустимо не более 5 активных инструментов")
    if (
        not enabled
        and context.instrument_enabled.get(uid, True)
        and len(context.tradable_instruments) <= MIN_TRADABLE
    ):
        raise ValueError("Для торговли должны остаться как минимум 2 активных инструмента")
    await context.repository.set_operational_value(
        f"instrument:{uid}:enabled", "true" if enabled else "false"
    )
    context.instrument_enabled[uid] = enabled
