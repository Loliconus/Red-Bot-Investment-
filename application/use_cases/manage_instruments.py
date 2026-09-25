"""Управление корзиной через MarketDataPort и RepositoryPort."""

from __future__ import annotations

from application.composition import AppContext
from core.domain.entities import Instrument

MAX_TRADABLE = 5
MIN_TRADABLE = 2


async def list_instrument_views(context: AppContext) -> list[dict[str, object]]:
    return [
        {
            "uid": i.uid,
            "ticker": i.ticker,
            "class_code": i.class_code,
            "lot_size": i.lot_size,
            "benchmark": i.is_benchmark,
            "enabled": context.instrument_enabled.get(i.uid, True),
        }
        for i in context.instruments
    ]


async def add_instrument(context: AppContext, ticker: str, class_code: str = "TQBR") -> Instrument:
    ticker = ticker.strip().upper()
    class_code = class_code.strip().upper()
    if not ticker or not class_code or len(ticker) > 15 or len(class_code) > 15:
        raise ValueError("Укажите корректный тикер и код класса")
    if len(context.tradable_instruments) >= MAX_TRADABLE:
        raise ValueError("Допустимо не более 5 торгуемых инструментов")
    instrument = await context.market_data.resolve_instrument(ticker, class_code)
    if any(i.uid == instrument.uid for i in context.instruments):
        raise ValueError("Инструмент уже добавлен")
    if instrument.is_benchmark or ticker == "IMOEX":
        raise ValueError("IMOEX — справочный бенчмарк, не торгуется")
    await context.repository.save_instrument(instrument)
    context.instruments.append(instrument)
    context.instrument_enabled[instrument.uid] = True
    return instrument


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
