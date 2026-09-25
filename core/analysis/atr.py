"""ATR (Average True Range) — база для волатильностных стопов и размеров позиции.

Истиный диапазон (True Range) —.maximum из трёх величин:
``H − L``, ``|H − C_prev|``, ``|L − C_prev|``.

ATR считается как среднее TR c Wilder-сглаживанием:
``ATR_t = (ATR_{t−1} × (n − 1) + TR_t) / n`` — это стандарт, применяемый
в TA-Lib и большинстве терминалов.
"""

from __future__ import annotations

from decimal import Decimal

from core.analysis.protocols import IndicatorResult
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries

ZERO = Decimal("0")
MIN_PERIOD = 2


def true_range(current: OHLCV, previous: OHLCV) -> Decimal:
    """Истинный диапазон одной свечи."""
    high_low = current.high - current.low
    high_prev_close = abs(current.high - previous.close)
    low_prev_close = abs(current.low - previous.close)
    return max(high_low, high_prev_close, low_prev_close)


def wilder_atr(series: CandleSeries, period: int) -> Decimal:
    """ATR по Wilder. Требуется минимум ``period`` свечей."""
    if period < MIN_PERIOD:
        msg = f"period должен быть >= {MIN_PERIOD}, получен {period}"
        raise ValueError(msg)

    candles = series.candles
    if len(candles) < period:
        msg = f"Недостаточно свечей: {len(candles)} < {period}"
        raise ValueError(msg)

    ranges = [true_range(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    first = sum(ranges[:period], ZERO) / Decimal(period)

    atr = first
    for value in ranges[period:]:
        atr = (atr * Decimal(period - 1) + value) / Decimal(period)
    return atr


def atr_pct(atr: Decimal, price: Decimal) -> Decimal:
    """ATR в долях от цены — сравнимо между инструментами."""
    if price == ZERO:
        return ZERO
    return atr / price


class ATRIndicator:
    """Индикатор ATR в контракте ``Indicator``."""

    name = "atr"

    __slots__ = ("period", "timeframe")

    def __init__(self, period: int = 14, *, timeframe: Timeframe = Timeframe.D1) -> None:
        self.period = period
        self.timeframe = timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        atr = wilder_atr(series, self.period)
        last_close = series.last.close if series.last else ZERO
        return IndicatorResult(
            value=atr,
            signal=f"atr_{self.period}",
            raw={"atr": atr, "atr_pct": atr_pct(atr, last_close)},
        )


def stop_distance(atr: Decimal, multiplier: Decimal) -> Decimal:
    """Расстояние до стопа = ATR × множитель."""
    return atr * multiplier
