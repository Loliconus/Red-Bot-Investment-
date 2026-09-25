"""Объёмные индикаторы: OBV и VWAP.

* **OBV** (On-Balance Volume) — кумулятивный объём: прибавляем объём, если
  закрытие выше предыдущего, вычитаем, если ниже. Расхождение OBV и цены
  (дивергенция) — ранний признак слабости тренда.
* **VWAP** (Volume Weighted Average Price) — средневзвешенная по объёму цена,
  внутридневной ориентир «справедливой» цены.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from core.analysis.protocols import IndicatorResult
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries

ZERO = Decimal("0")
TWO = Decimal("2")


def on_balance_volume(series: CandleSeries) -> Decimal:
    """Значение OBV на конец серии."""
    candles = series.candles
    if not candles:
        return ZERO

    obv = ZERO
    for previous, current in pairwise(candles):
        if current.close > previous.close:
            obv += Decimal(current.volume)
        elif current.close < previous.close:
            obv -= Decimal(current.volume)
    return obv


def obv_slope(series: CandleSeries, *, lookback: int = 5) -> Decimal:
    """Наклон OBV на последних ``lookback`` свечах: >0 — приток, <0 — отток."""
    candles = series.candles
    if len(candles) < lookback + 1:
        return ZERO
    tail = candles[-(lookback + 1) :]
    return on_balance_volume(CandleSeries(timeframe=series.timeframe, candles=tuple(tail)))


def vwap(series: CandleSeries) -> Decimal:
    """VWAP по всей серии через typical price."""
    total_pv = ZERO
    total_volume = ZERO
    for candle in series.candles:
        total_pv += candle.typical_price * Decimal(candle.volume)
        total_volume += Decimal(candle.volume)
    if total_volume == ZERO:
        return ZERO
    return total_pv / total_volume


def anchored_vwap(series: CandleSeries, *, anchor_timestamp: object) -> Decimal:
    """VWAP от точки якора — например, от начала дня или от локального минимума."""
    filtered = tuple(c for c in series.candles if c.timestamp >= anchor_timestamp)  # type: ignore[operator]
    if not filtered:
        return ZERO
    return vwap(CandleSeries(timeframe=series.timeframe, candles=filtered))


def volume_trend(series: CandleSeries, *, lookback: int = 10) -> Decimal:
    """Отношение объёма последней свечи к среднему за ``lookback``."""
    candles = series.candles
    if len(candles) < lookback + 1:
        return ZERO
    recent = Decimal(candles[-1].volume)
    average = sum((Decimal(c.volume) for c in candles[-(lookback + 1) : -1]), ZERO) / Decimal(
        lookback
    )
    if average == ZERO:
        return ZERO
    return recent / average


class OBVIndicator:
    name = "obv"

    __slots__ = ("lookback", "timeframe")

    def __init__(self, lookback: int = 5, *, timeframe: Timeframe = Timeframe.M1) -> None:
        self.lookback = lookback
        self.timeframe = timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        slope = obv_slope(series, lookback=self.lookback)
        signal = "obv_rising" if slope > ZERO else "obv_falling" if slope < ZERO else "obv_flat"
        return IndicatorResult(
            value=slope,
            signal=signal,
            raw={"obv": on_balance_volume(series), "obv_slope": slope},
        )


class VWAPIndicator:
    name = "vwap"

    __slots__ = ("timeframe",)

    def __init__(self, *, timeframe: Timeframe = Timeframe.M1) -> None:
        self.timeframe = timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        value = vwap(series)
        last_close = series.last.close if series.last else ZERO
        deviation = (last_close - value) / value if value != ZERO else ZERO
        signal = (
            "above_vwap"
            if last_close > value
            else "below_vwap"
            if last_close < value
            else "at_vwap"
        )
        return IndicatorResult(
            value=value, signal=signal, raw={"vwap": value, "deviation": deviation}
        )


def price_vs_vwap(candle: OHLCV, vwap_value: Decimal) -> Decimal:
    """Отклонение цены от VWAP в долях."""
    if vwap_value == ZERO:
        return ZERO
    return (candle.close - vwap_value) / vwap_value
