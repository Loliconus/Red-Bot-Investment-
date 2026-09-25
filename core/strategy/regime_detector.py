"""Определение рыночного режима.

Режим — контекст, а не сигнал. Одна и та же торговая идея ведёт себя
принципиально differently в тренде и в боковике: от этого зависят размер стопа,
цель и даже сама допустимость входа.

Три режима:
* ``TRENDING`` — направленное движение, есть наклон и «лестница» экстремумов;
* ``RANGING`` — цена ходит в коридоре, наклон около нуля;
* ``HIGH_VOLATILITY`` — ATR в процентах аномально высок, любой стоп выбивается
  шумом.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.analysis.atr import atr_pct, wilder_atr
from core.domain.enums import MarketRegime, Timeframe, Trend, VolatilityRegime
from core.domain.value_objects import CandleSeries

ZERO = Decimal("0")
ONE = Decimal("1")

#: Наклон (в ATR за свечу), начиная с которого движение считается направленным.
TREND_SLOPE_ATR = Decimal("0.15")
#: ATR в процентах, начиная с которого режим считается высоковолатильным.
HIGH_VOL_PCT = Decimal("0.03")
#: ATR в процентах, ниже которого режим считается спокойным.
LOW_VOL_PCT = Decimal("0.008")
#: Минимальное число свечей для осмысленного наклона.
MIN_LOOKBACK = 10


def normalized_slope(series: CandleSeries, *, lookback: int = 20) -> Decimal:
    """Наклон цены за ``lookback`` свечей, нормированный на ATR.

    ``(последняя цена − цена lookback назад) / (lookback × ATR)``.
    Значение > 1 означает движение более чем на ATR за свечу — сильный тренд.
    """
    window = series.tail(lookback)
    if len(window) < MIN_LOOKBACK:
        return ZERO

    atr = wilder_atr(series, min(14, len(series) - 1))
    if atr == ZERO:
        return ZERO

    span = len(window) - 1
    return (window[-1].close - window[0].close) / (atr * Decimal(span))


def higher_highs_lows(series: CandleSeries, *, lookback: int = 10) -> bool:
    """«Лестница» восходящей структуры: каждый максимум и минимум выше предыдущих."""
    window = series.tail(lookback)
    if len(window) < 3:
        return False

    half = len(window) // 2
    first_highs = [c.high for c in window[:half]]
    second_highs = [c.high for c in window[half:]]
    first_lows = [c.low for c in window[:half]]
    second_lows = [c.low for c in window[half:]]

    return max(second_highs) > max(first_highs) and min(second_lows) > min(first_lows)


def volatility_regime(value_pct: Decimal) -> VolatilityRegime:
    if value_pct >= HIGH_VOL_PCT:
        return VolatilityRegime.HIGH
    if value_pct <= LOW_VOL_PCT:
        return VolatilityRegime.LOW
    return VolatilityRegime.NORMAL


@dataclass(frozen=True, slots=True, kw_only=True)
class RegimeState:
    """Состояние рынка на одном таймфрейме."""

    timeframe: Timeframe
    regime: MarketRegime
    trend: Trend
    volatility: VolatilityRegime
    slope_atr: Decimal
    atr_value: Decimal
    atr_pct: Decimal
    structure_confirmed: bool
    confidence: Decimal

    def is_favorable_for_long(self) -> bool:
        """Подходит ли режим для long-сетапа."""
        return (
            self.regime is MarketRegime.TRENDING
            and self.trend is Trend.UP
            and self.volatility is not VolatilityRegime.HIGH
        )


def detect_regime(
    series: CandleSeries,
    *,
    lookback: int = 20,
    atr_period: int = 14,
) -> RegimeState:
    """Определяет режим по серии свечей.

    Логика:
    1. считаем ATR и нормированный наклон;
    2. волатильность → ``HIGH`` / ``NORMAL`` / ``LOW``;
    3. если |наклон| выше порога и структура подтверждена — тренд;
    4. иначе — боковик (или высоковолатильный режим, если ATR зашкаливает).
    """
    if len(series) < max(atr_period, MIN_LOOKBACK):
        msg = f"Недостаточно свечей: {len(series)} < {max(atr_period, MIN_LOOKBACK)}"
        raise ValueError(msg)

    atr = wilder_atr(series, atr_period)
    value_pct = atr_pct(atr, series.last.close) if series.last else ZERO
    slope = normalized_slope(series, lookback=lookback)
    structure_up = higher_highs_lows(series, lookback=lookback)
    structure_down = higher_highs_lows(_invert(series), lookback=lookback)
    vol = volatility_regime(value_pct)

    if vol is VolatilityRegime.HIGH and abs(slope) < TREND_SLOPE_ATR:
        regime = MarketRegime.HIGH_VOLATILITY
        trend = Trend.FLAT
    elif abs(slope) >= TREND_SLOPE_ATR:
        regime = MarketRegime.TRENDING
        trend = Trend.UP if slope > ZERO else Trend.DOWN
    else:
        regime = MarketRegime.RANGING
        trend = Trend.FLAT

    if regime is MarketRegime.TRENDING:
        structure_confirmed = structure_up if trend is Trend.UP else structure_down
    else:
        structure_confirmed = False

    confidence = _confidence(slope, structure_confirmed, vol, regime)

    return RegimeState(
        timeframe=series.timeframe,
        regime=regime,
        trend=trend,
        volatility=vol,
        slope_atr=slope,
        atr_value=atr,
        atr_pct=value_pct,
        structure_confirmed=structure_confirmed,
        confidence=confidence,
    )


def _invert(series: CandleSeries) -> CandleSeries:
    """Зеркалит серию: позволяет переиспользовать проверку «лестницы» для нисходящего тренда."""
    from core.domain.value_objects import OHLCV

    inverted = tuple(
        OHLCV(
            open=-c.open,
            high=-c.low,
            low=-c.high,
            close=-c.close,
            volume=c.volume,
            timestamp=c.timestamp,
            timeframe=c.timeframe,
        )
        for c in series.candles
    )
    return CandleSeries(timeframe=series.timeframe, candles=inverted)


def _confidence(
    slope: Decimal,
    structure_confirmed: bool,
    vol: VolatilityRegime,
    regime: MarketRegime,
) -> Decimal:
    base = min(abs(slope) / TREND_SLOPE_ATR, ONE) if slope != ZERO else Decimal("0.5")
    bonus = Decimal("0.2") if structure_confirmed else ZERO
    penalty = Decimal("0.2") if vol is VolatilityRegime.HIGH else ZERO
    flat_bonus = Decimal("0.3") if regime is MarketRegime.RANGING else ZERO
    value = base + bonus - penalty + flat_bonus
    return max(ZERO, min(ONE, value))
