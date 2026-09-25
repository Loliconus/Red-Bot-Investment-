"""Контракт индикатора — единый для всех собственных расчётов в ``core``.

Собственные индикаторы считаются только там, где API их не даёт:
ATR, OBV, VWAP, уровни Фибоначчи, confluence-скор, корреляция с IMOEX,
анализ стакана. SMA/EMA/RSI/MACD/Bollinger **не дублируются** в ``core`` —
они запрашиваются у API через ``MarketDataPort.get_api_indicator``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True, kw_only=True)
class IndicatorResult:
    """Результат расчёта индикатора: число, сигнал и все промежуточные серии."""

    value: Decimal
    signal: str = ""
    raw: Mapping[str, Decimal] = field(default_factory=dict)

    def raw_or_zero(self, key: str) -> Decimal:
        return self.raw.get(key, ZERO)


@runtime_checkable
class Indicator(Protocol):
    """Индикатор, вычисляемый по последовательности свечей."""

    name: str
    timeframe: Timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        """Считает значение. Не делает I/O и не зависит от времени."""
        ...
