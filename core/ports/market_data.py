"""Порт рыночных данных.

Закрытый перечень портов проекта: порт создаётся только тогда, когда для него
реально существует вторая реализация. У ``MarketDataPort`` их три:
``TBankMarketDataAdapter``, ``SandboxMarketDataAdapter``, ``BacktestReplayAdapter``.

Порт — ``typing.Protocol``, без наследования от ``abc.ABC``: адаптер
соответствует порту структурно, просто имея нужные методы.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from core.domain.catalog import InstrumentCatalogEntry
from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, OrderbookSnapshot


@runtime_checkable
class MarketDataPort(Protocol):
    """Источник котировок, свечей, стакана, индикаторов и справочника инструментов."""

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        from_: datetime,
        to: datetime,
    ) -> list[OHLCV]:
        """Исторические свечи. Диапазон обязан быть tz-aware в UTC."""
        ...

    def stream_candles(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
    ) -> AsyncIterator[OHLCV]:
        """Поток свечей. Адаптер обязан сам переподписываться при обрыве.

        Объявлен как обычный ``def``, возвращающий асинхронный итератор:
        именно так ``async for``-адаптеры (и ``async def ... yield``) стыкуются
        с протоколом без расхождения в типе корутины.
        """
        ...

    async def get_orderbook(
        self,
        instrument: Instrument,
        depth: int = 20,
    ) -> OrderbookSnapshot: ...

    async def get_api_indicator(
        self,
        instrument: Instrument,
        indicator: str,
        timeframe: Timeframe,
        params: Mapping[str, Any],
    ) -> dict[str, Decimal | None]:
        """Индикаторы API в Decimal: ``{"sma": Decimal("275.13")}`` и т. п."""
        ...

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        """Однозначно находит инструмент. Кидает исключение, если найдено несколько."""
        ...

    async def fetch_catalog(
        self, instrument_types: Sequence[str] | None = None
    ) -> list[InstrumentCatalogEntry]:
        """Справочник инструментов из API (``InstrumentsService``: Shares/Etfs/...).

        Вызывающий код сохраняет результат в хранилище: списки API жёстко
        ограничены по частоте (15 запросов в минуту на метод), поэтому читать
        каталог нужно из БД, а не из сети. Оффлайн-контуры (бэктест) сетевого
        справочника не имеют и сообщают об этом ``CatalogUnavailableError``.
        """
        ...

    async def search_instruments(
        self,
        query: str,
        *,
        instrument_type: str | None = None,
        limit: int = 20,
    ) -> list[InstrumentCatalogEntry]:
        """Поиск инструмента по тикеру, названию или ISIN (``FindInstrument``)."""
        ...
