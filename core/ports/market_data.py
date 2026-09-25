"""Порт рыночных данных.

Закрытый перечень портов проекта: порт создаётся только тогда, когда для него
реально существует вторая реализация. У ``MarketDataPort`` их три:
``TBankMarketDataAdapter``, ``SandboxMarketDataAdapter``, ``BacktestReplayAdapter``.

Порт — ``typing.Protocol``, без наследования от ``abc.ABC``: адаптер
соответствует порту структурно, просто имея нужные методы.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from core.domain.entities import Instrument
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, OrderbookSnapshot


@runtime_checkable
class MarketDataPort(Protocol):
    """Источник котировок, свечей, стакана и индикаторов из API."""

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
    ) -> dict[str, float | None]:
        """Индикаторы, предоставляемые API: SMA, EMA, RSI, MACD, Bollinger.

        Возвращает словарь ``имя серии -> значение`` (например ``{"sma": 275.13}``
        или ``{"macd": ..., "signal": ..., "histogram": ...}``).
        """
        ...

    async def resolve_instrument(self, ticker: str, class_code: str) -> Instrument:
        """Однозначно находит инструмент. Кидает исключение, если найдено несколько."""
        ...
