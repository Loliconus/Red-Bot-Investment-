"""Реестр собственных индикаторов.

Позволяет собирать вычислительный пайплайн из конфигурации, а не из
захардкоженных вызовов: ``register(ATRIndicator(period=14))`` →
``calculate_all(series)`` → ``{"atr": IndicatorResult(...)}``.

Реестр считает только то, чего нет в API (ATR, OBV, VWAP, Фибоначчи,
корреляция с IMOEX, стакан). SMA/EMA/RSI/MACD/Bollinger запрашиваются у API.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.analysis.atr import ATRIndicator
from core.analysis.fibonacci import FibonacciIndicator
from core.analysis.market_correlation import MarketCorrelationIndicator
from core.analysis.orderbook_analysis import OrderbookIndicator
from core.analysis.protocols import Indicator, IndicatorResult
from core.analysis.volume_indicators import OBVIndicator, VWAPIndicator
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries, OrderbookSnapshot


class IndicatorRegistry:
    """Простой реестр индикаторов с расчётом «всех сразу»."""

    __slots__ = ("_indicators",)

    def __init__(self) -> None:
        self._indicators: dict[str, Indicator] = {}

    def register(self, indicator: Indicator) -> None:
        self._indicators[indicator.name] = indicator

    def unregister(self, name: str) -> None:
        self._indicators.pop(name, None)

    def get(self, name: str) -> Indicator | None:
        return self._indicators.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._indicators))

    def calculate_all(
        self,
        series: CandleSeries,
        *,
        only: Iterable[str] | None = None,
    ) -> dict[str, IndicatorResult]:
        """Считает все индикаторы, подходящие по таймфрейму серии.

        Ошибка одного индикатора не роняет весь пайплайн: результат просто
        отсутствует в словаре, а сбой виден по отсутствующему ключу.
        """
        selected = set(only) if only is not None else None
        results: dict[str, IndicatorResult] = {}
        for name, indicator in self._indicators.items():
            if selected is not None and name not in selected:
                continue
            if indicator.timeframe is not series.timeframe:
                continue
            try:
                results[name] = indicator.calculate(series)
            except ValueError:
                # Недостаточно истории — нормальная ситуация на старте.
                continue
        return results

    def __len__(self) -> int:
        return len(self._indicators)


def build_default_registry(
    *,
    benchmark_d1: CandleSeries | None = None,
    orderbook: OrderbookSnapshot | None = None,
    atr_period: int = 14,
    obv_lookback: int = 5,
    fib_lookback: int = 30,
) -> IndicatorRegistry:
    """Собирает реестр с набором индикаторов по умолчанию."""
    registry = IndicatorRegistry()

    registry.register(ATRIndicator(period=atr_period, timeframe=Timeframe.D1))
    registry.register(ATRIndicator(period=atr_period, timeframe=Timeframe.H1))
    registry.register(OBVIndicator(lookback=obv_lookback, timeframe=Timeframe.M1))
    registry.register(VWAPIndicator(timeframe=Timeframe.M1))
    registry.register(FibonacciIndicator(lookback=fib_lookback, timeframe=Timeframe.H1))

    if benchmark_d1 is not None:
        registry.register(MarketCorrelationIndicator(benchmark_d1, timeframe=Timeframe.D1))
    if orderbook is not None:
        registry.register(OrderbookIndicator(orderbook))

    return registry
