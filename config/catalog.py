"""Каталог популярных ликвидных инструментов Московской Биржи (акции, режим TQBR).

Используется для:
1. Удобного выбора и автодополнения инструментов в GUI без необходимости
   вручную помнить точные тикеры, класс-коды и размеры лотов.
2. Безопасного локального разрешения параметров инструментов (размер лота, UID)
   при недоступности внешнего API или в контуре песочницы/бэктеста.
"""

from __future__ import annotations

import re
from typing import Any

from core.domain.entities import Instrument

MOEX_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "ticker": "SBER",
        "name": "Сбербанк",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004730N88",
        "currency": "RUB",
    },
    {
        "ticker": "GAZP",
        "name": "Газпром",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004730RP0",
        "currency": "RUB",
    },
    {
        "ticker": "LKOH",
        "name": "ЛУКОЙЛ",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004731032",
        "currency": "RUB",
    },
    {
        "ticker": "VTBR",
        "name": "Банк ВТБ",
        "class_code": "TQBR",
        "lot_size": 10000,
        "uid": "BBG004730ZJ9",
        "currency": "RUB",
    },
    {
        "ticker": "YNDX",
        "name": "Яндекс",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG006L8G4H6",
        "currency": "RUB",
    },
    {
        "ticker": "ROSN",
        "name": "Роснефть",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004731354",
        "currency": "RUB",
    },
    {
        "ticker": "NVTK",
        "name": "НОВАТЭК",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00475KKY8",
        "currency": "RUB",
    },
    {
        "ticker": "GMKN",
        "name": "ГМК Норникель",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004731489",
        "currency": "RUB",
    },
    {
        "ticker": "TATN",
        "name": "Татнефть",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004RVFFC0",
        "currency": "RUB",
    },
    {
        "ticker": "MGNT",
        "name": "Магнит",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004S68473",
        "currency": "RUB",
    },
    {
        "ticker": "MOEX",
        "name": "Московская Биржа",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004730JJ5",
        "currency": "RUB",
    },
    {
        "ticker": "CHMF",
        "name": "Северсталь",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00475K2X9",
        "currency": "RUB",
    },
    {
        "ticker": "NLMK",
        "name": "НЛМК",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S681W1",
        "currency": "RUB",
    },
    {
        "ticker": "ALRS",
        "name": "АЛРОСА",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S68B31",
        "currency": "RUB",
    },
    {
        "ticker": "PLZL",
        "name": "Полюс",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG000R607Y3",
        "currency": "RUB",
    },
    {
        "ticker": "SNGS",
        "name": "Сургутнефтегаз",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG0047315D0",
        "currency": "RUB",
    },
    {
        "ticker": "SNGSP",
        "name": "Сургутнефтегаз (прив.)",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG0047315Y7",
        "currency": "RUB",
    },
    {
        "ticker": "PHOR",
        "name": "ФосАгро",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004S68598",
        "currency": "RUB",
    },
    {
        "ticker": "AFLT",
        "name": "Аэрофлот",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S683W7",
        "currency": "RUB",
    },
    {
        "ticker": "OZON",
        "name": "Озон",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00Y91R9T3",
        "currency": "RUB",
    },
    {
        "ticker": "VKCO",
        "name": "ВК (VK)",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00178PGX3",
        "currency": "RUB",
    },
    {
        "ticker": "POSI",
        "name": "Группа Позитив",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00ZCH21B9",
        "currency": "RUB",
    },
    {
        "ticker": "TRNFP",
        "name": "Транснефть (прив.)",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004730ZJ8",
        "currency": "RUB",
    },
    {
        "ticker": "MAGN",
        "name": "ММК",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S68507",
        "currency": "RUB",
    },
    {
        "ticker": "CBOM",
        "name": "МКБ",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG008F2GWD2",
        "currency": "RUB",
    },
    {
        "ticker": "RUAL",
        "name": "РУСАЛ",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG0088X4504",
        "currency": "RUB",
    },
    {
        "ticker": "MTSS",
        "name": "МТС",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S682Z6",
        "currency": "RUB",
    },
    {
        "ticker": "T",
        "name": "Т-Технологии (ТКС)",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG00QPYJ5H0",
        "currency": "RUB",
    },
    {
        "ticker": "IRAO",
        "name": "Интер РАО",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG004S68614",
        "currency": "RUB",
    },
    {
        "ticker": "PIKK",
        "name": "ПИК",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG004S68BH9",
        "currency": "RUB",
    },
    {
        "ticker": "RTKM",
        "name": "Ростелеком",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S68758",
        "currency": "RUB",
    },
    {
        "ticker": "HYDR",
        "name": "РусГидро",
        "class_code": "TQBR",
        "lot_size": 1000,
        "uid": "BBG00475KHX6",
        "currency": "RUB",
    },
    {
        "ticker": "SELG",
        "name": "Селигдар",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S68311",
        "currency": "RUB",
    },
    {
        "ticker": "UPRO",
        "name": "Юнипро",
        "class_code": "TQBR",
        "lot_size": 1000,
        "uid": "BBG00475K280",
        "currency": "RUB",
    },
    {
        "ticker": "BSPB",
        "name": "Банк Санкт-Петербург",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG004S681B4",
        "currency": "RUB",
    },
    {
        "ticker": "AFKS",
        "name": "АФК Система",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG004S686N0",
        "currency": "RUB",
    },
    {
        "ticker": "SOFL",
        "name": "Софтлайн",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG0135T76T3",
        "currency": "RUB",
    },
    {
        "ticker": "SVCB",
        "name": "Совкомбанк",
        "class_code": "TQBR",
        "lot_size": 100,
        "uid": "BBG016P3Y6S8",
        "currency": "RUB",
    },
    {
        "ticker": "ASTR",
        "name": "Группа Астра",
        "class_code": "TQBR",
        "lot_size": 1,
        "uid": "BBG016335372",
        "currency": "RUB",
    },
    {
        "ticker": "FLOT",
        "name": "Совкомфлот",
        "class_code": "TQBR",
        "lot_size": 10,
        "uid": "BBG00Y2K4Z00",
        "currency": "RUB",
    },
)

_CATALOG_BY_TICKER: dict[str, dict[str, Any]] = {item["ticker"]: item for item in MOEX_CATALOG}


def get_catalog_instruments() -> list[dict[str, Any]]:
    """Возвращает справочный список инструментов для GUI."""
    return list(MOEX_CATALOG)


def get_catalog_name(ticker: str) -> str:
    """Возвращает русскоязычное название компании для тикера."""
    item = _CATALOG_BY_TICKER.get(ticker.upper())
    return str(item["name"]) if item else ticker.upper()


def extract_ticker(text: str) -> str:
    """Извлекает чистый тикер из ввода пользователя (например, 'VTBR (Банк ВТБ)' → 'VTBR')."""
    text = text.strip()
    match = re.search(r"\b([A-Z]{1,10})\b", text.upper())
    if match:
        found = match.group(1)
        if found in _CATALOG_BY_TICKER:
            return found

    for item in MOEX_CATALOG:
        if item["name"].lower() in text.lower():
            return str(item["ticker"])

    clean = re.sub(r"[^A-Za-z0-9_-]", "", text).upper()
    return clean or text.upper()


def find_catalog_instrument(ticker_or_query: str, class_code: str = "TQBR") -> Instrument | None:
    """Ищет инструмент в каталоге Мосбиржи по тикеру или названию."""
    ticker = extract_ticker(ticker_or_query)
    class_code = class_code.strip().upper() or "TQBR"

    item = _CATALOG_BY_TICKER.get(ticker)
    if item is not None:
        return Instrument(
            uid=item["uid"],
            ticker=item["ticker"],
            lot_size=int(item["lot_size"]),
            class_code=class_code or item["class_code"],
            is_benchmark=False,
            currency=item["currency"],
        )

    for entry in MOEX_CATALOG:
        if ticker.lower() in entry["name"].lower():
            return Instrument(
                uid=entry["uid"],
                ticker=entry["ticker"],
                lot_size=int(entry["lot_size"]),
                class_code=class_code or entry["class_code"],
                is_benchmark=False,
                currency=entry["currency"],
            )

    return None
