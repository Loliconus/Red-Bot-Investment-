"""Управление корзиной через MarketDataPort и RepositoryPort."""

from __future__ import annotations

import re

from application.composition import AppContext
from application.use_cases.manage_instrument_catalog import (
    get_catalog_name,
    search_instruments,
)
from core.domain.entities import Instrument

MAX_TRADABLE = 5
MIN_TRADABLE = 2

#: Тикер Мосбиржи: латиница, цифры, дефис и подчёркивание.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,14}$")


def extract_ticker(text: str) -> str:
    """Извлекает чистый тикер из ввода пользователя ('VTBR (Банк ВТБ)' → 'VTBR').

    Проверка по каталогу здесь невозможна и не нужна: каталог живёт в API и БД,
    а разрешением символа занимается ``resolve_instrument``. Если ввода похож на
    название компании, а не на тикер, поиск выполняется через каталог.
    """
    cleaned = text.strip()
    match = re.search(r"\b([A-Z]{1,10})\b", cleaned.upper())
    if match:
        return match.group(1)
    fallback = re.sub(r"[^A-Za-z0-9_-]", "", cleaned).upper()
    return fallback or cleaned.upper()


def _looks_like_ticker(value: str) -> bool:
    return bool(_TICKER_PATTERN.fullmatch(value))


async def _resolve_from_input(context: AppContext, ticker: str, class_code: str) -> Instrument:
    """Разрешает ввод пользователя: тикер — напрямую, название — через поиск."""
    symbol = extract_ticker(ticker).strip().upper()
    if _looks_like_ticker(symbol):
        return await context.market_data.resolve_instrument(symbol, class_code)

    found = await search_instruments(context, ticker.strip(), limit=5)
    for entry in found:
        if not _looks_like_ticker(entry.ticker.upper()):
            continue
        return await context.market_data.resolve_instrument(
            entry.ticker, entry.class_code or class_code
        )
    msg = f"Инструмент не найден по запросу '{ticker}': укажите тикер (например, SBER)"
    raise ValueError(msg)


async def list_instrument_views(context: AppContext) -> list[dict[str, object]]:
    return [
        {
            "uid": i.uid,
            "ticker": i.ticker,
            "name": await get_catalog_name(context, i.ticker),
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

    instrument = await _resolve_from_input(context, raw_ticker, class_code)
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
