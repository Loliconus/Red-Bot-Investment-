"""Каталог инструментов: получить из API, сохранить, читать из хранилища.

Раньше справочник жил в ``config/catalog.py`` константами — и молча устаревал:
менялись лоты, делистинги и UID, а код продолжал использовать неверные данные.
Теперь единственный источник каталога — T-Invest API
(``InstrumentsService``: Shares/Etfs/Currencies/Futures/Bonds и FindInstrument),
а сохранённая копия живёт в БД (таблица ``instrument_catalog``).

Почему именно так:

* списки API ограничены 15 запросами в минуту — читать их на каждый экран GUI
  нельзя, поэтому каталог загружается пачкой и кешируется;
* бэктест и оффлайн-режимы не имеют сети, но должны видеть те же инструменты:
  они читают сохранённый каталог;
* ``resolve_instrument`` по-прежнему ходит в API: локальная запись не должна
  подменять собой проверку инструмента в боевом/песочном контуре.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from core.domain.catalog import DEFAULT_CATALOG_TYPES, InstrumentCatalogEntry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from application.composition import AppContext

logger = structlog.get_logger(__name__)

#: Ключ в ``operational_settings``: момент последнего обновления каталога.
CATALOG_UPDATED_AT_KEY = "instrument_catalog:updated_at"

#: Каталог меняется редко (делистинги, сплиты, новые листинги), поэтому
#: обновляем не на каждый старт, а не чаще раза в сутки.
CATALOG_MAX_AGE = timedelta(hours=24)

#: Сколько записей отдаём в GUI по умолчанию.
DEFAULT_CATALOG_LIMIT = 200


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    """Итог обновления каталога из API."""

    types: tuple[str, ...]
    fetched: int
    updated_at: datetime


async def refresh_instrument_catalog(
    context: AppContext,
    *,
    instrument_types: Sequence[str] | None = None,
) -> CatalogRefreshResult:
    """Загружает справочник из API и сохраняет его в БД.

    Записи обновляемых типов удаляются перед вставкой: иначе делистинг или
    смена лотности оставили бы в каталоге несуществующие бумаги.
    """
    types = tuple(
        dict.fromkeys(item.strip().lower() for item in (instrument_types or DEFAULT_CATALOG_TYPES))
    )
    entries = await context.market_data.fetch_catalog(types)
    await context.repository.delete_catalog_entries(types)
    await context.repository.save_catalog_entries(entries)
    updated_at = datetime.now(tz=UTC)
    await context.repository.set_operational_value(CATALOG_UPDATED_AT_KEY, updated_at.isoformat())
    logger.info("instrument_catalog_refreshed", types=list(types), entries=len(entries))
    return CatalogRefreshResult(types=types, fetched=len(entries), updated_at=updated_at)


async def catalog_updated_at(context: AppContext) -> datetime | None:
    """Момент последнего успешного обновления каталога из API."""
    raw = await context.repository.get_operational_value(CATALOG_UPDATED_AT_KEY)
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def is_catalog_stale(context: AppContext, *, max_age: timedelta = CATALOG_MAX_AGE) -> bool:
    """Пустой или просроченный каталог требует обновления из API."""
    updated_at = await catalog_updated_at(context)
    if updated_at is None:
        return True
    return datetime.now(tz=UTC) - updated_at > max_age


async def ensure_catalog_fresh(
    context: AppContext, *, max_age: timedelta = CATALOG_MAX_AGE
) -> bool:
    """Обновляет каталог, только если он устарел. Возвращает факт обновления."""
    if not await is_catalog_stale(context, max_age=max_age):
        return False
    await refresh_instrument_catalog(context)
    return True


async def list_catalog_entries(
    context: AppContext,
    *,
    query: str | None = None,
    instrument_types: Sequence[str] | None = None,
    tradable_only: bool = True,
    limit: int = DEFAULT_CATALOG_LIMIT,
    offset: int = 0,
) -> list[InstrumentCatalogEntry]:
    """Читает каталог из БД: GUI не обращается к API за списком."""
    return await context.repository.list_catalog_entries(
        query=query,
        instrument_types=instrument_types,
        tradable_only=tradable_only,
        limit=limit,
        offset=offset,
    )


async def search_instruments(
    context: AppContext,
    query: str,
    *,
    instrument_type: str | None = None,
    limit: int = 20,
) -> list[InstrumentCatalogEntry]:
    """Ищет инструмент: сначала в сохранённом каталоге, затем через FindInstrument.

    Найденное через API сохраняется — повторный поиск той же бумаги уже не
    требует сети.
    """
    needle = query.strip()
    if len(needle) < 2:
        return []
    local = await context.repository.list_catalog_entries(
        query=needle,
        instrument_types=[instrument_type] if instrument_type else None,
        tradable_only=False,
        limit=limit,
    )
    results: dict[str, InstrumentCatalogEntry] = {entry.uid: entry for entry in local}
    if results:
        # Каталог — основной источник: сеть трогаем только для неизвестных бумаг.
        return list(results.values())[:limit]

    remote = await context.market_data.search_instruments(
        needle, instrument_type=instrument_type, limit=limit
    )
    fresh = [entry for entry in remote if entry.uid and entry.uid not in results]
    if fresh:
        await context.repository.save_catalog_entries(fresh)
        results.update({entry.uid: entry for entry in fresh})
    return list(results.values())[:limit]


async def get_catalog_name(context: AppContext, ticker: str) -> str:
    """Русскоязычное название инструмента из сохранённого каталога."""
    entry = await context.repository.find_catalog_entry(ticker)
    if entry is not None and entry.name:
        return entry.name
    return ticker.strip().upper()


async def catalog_status(context: AppContext) -> dict[str, object]:
    """Состояние каталога для GUI: количество записей и возраст данных."""
    count = await context.repository.count_catalog_entries()
    updated_at = await catalog_updated_at(context)
    return {
        "count": count,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "stale": await is_catalog_stale(context),
    }


def catalog_view(entry: InstrumentCatalogEntry) -> dict[str, object]:
    """Запись каталога в формате, пригодном для GUI и JSON API."""
    return {
        "uid": entry.uid,
        "ticker": entry.ticker,
        "name": entry.name,
        "class_code": entry.class_code,
        "lot_size": entry.lot_size,
        "currency": entry.currency or "—",
        "instrument_type": entry.instrument_type,
        "isin": entry.isin,
        "tradable": entry.tradable,
        "label": entry.label,
    }


async def list_catalog_views(
    context: AppContext,
    *,
    query: str | None = None,
    instrument_types: Sequence[str] | None = None,
    tradable_only: bool = True,
    limit: int = DEFAULT_CATALOG_LIMIT,
) -> list[dict[str, object]]:
    """Каталог для шаблонов GUI и JSON-эндпоинтов."""
    entries = await list_catalog_entries(
        context,
        query=query,
        instrument_types=instrument_types,
        tradable_only=tradable_only,
        limit=limit,
    )
    return [catalog_view(entry) for entry in entries]
