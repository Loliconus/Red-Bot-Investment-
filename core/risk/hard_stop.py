"""Hard stop — обязательный ценовой стоп.

Каждая позиция **всегда** имеет «железный» стоп. Это не обсуждается: пока
позиция открыта, стоп уже выставлен.

Стоп ставится ниже структурного минимума на величину ATR-буфера, чтобы
случайный выброс по рыночному шуму не выбивал позицию раньше времени, а
«истинное» пробой структуры — выбивал.

Дополнительно реализован трейлинг-стоп: после достижения 1R стоп подтягивается
вслед за ценой на расстоянии ``atr_multiplier × ATR`` от достигнутого максимума.
Функция трейлинга никогда не двигает стоп вниз.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.analysis.atr import wilder_atr
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries

ZERO = Decimal("0")
ONE = Decimal("1")

#: Стандартный буфер под структуру в ATR.
DEFAULT_ATR_MULTIPLIER = Decimal("1.5")
#: Минимальный буфер — защита от «стопа в миллиметре от входа».
MIN_STOP_DISTANCE_PCT = Decimal("0.005")


def hard_stop_price(
    *,
    entry_price: Decimal,
    structural_low: Decimal,
    atr: Decimal,
    atr_multiplier: Decimal = DEFAULT_ATR_MULTIPLIER,
) -> Decimal:
    """Цена hard stop: структурный минимум минус ATR-буфер."""
    if structural_low >= entry_price:
        msg = f"Структурный минимум ({structural_low}) обязан быть ниже входа ({entry_price})"
        raise ValueError(msg)
    if atr < ZERO:
        msg = f"ATR не может быть отрицательным: {atr}"
        raise ValueError(msg)

    stop = structural_low - atr * atr_multiplier
    # Стоп не должен быть ближе к входу, чем MIN_STOP_DISTANCE_PCT:
    # иначе любой рыночный шум выбьет позицию ещё до начала движения.
    floor_level = entry_price * (ONE - MIN_STOP_DISTANCE_PCT)
    return min(stop, floor_level)


def is_hard_stop_triggered(*, current_price: Decimal, stop_price: Decimal) -> bool:
    """Пробит ли стоп (long: цена упала до стопа или ниже)."""
    return current_price <= stop_price


def trailing_stop_price(
    *,
    current_stop: Decimal,
    highest_price_since_entry: Decimal,
    atr: Decimal,
    atr_multiplier: Decimal = DEFAULT_ATR_MULTIPLIER,
) -> Decimal:
    """Новое значение трейлинг-стопа. Никогда не ниже текущего."""
    candidate = highest_price_since_entry - atr * atr_multiplier
    return max(current_stop, candidate)


def distance_in_atr(*, entry_price: Decimal, stop_price: Decimal, atr: Decimal) -> Decimal:
    """Расстояние до стопа в ATR — удобная мера «ширины» стопа."""
    if atr == ZERO:
        return ZERO
    return (entry_price - stop_price) / atr


@dataclass(frozen=True, slots=True, kw_only=True)
class StopCheckResult:
    """Результат проверки стопа."""

    triggered: bool
    stop_price: Decimal
    current_price: Decimal


def check_hard_stop(
    *,
    current_price: Decimal,
    stop_price: Decimal,
) -> StopCheckResult:
    return StopCheckResult(
        triggered=is_hard_stop_triggered(current_price=current_price, stop_price=stop_price),
        stop_price=stop_price,
        current_price=current_price,
    )


def structural_stop_from_series(
    series: CandleSeries,
    *,
    lookback: int = 20,
    atr_multiplier: Decimal = DEFAULT_ATR_MULTIPLIER,
    atr_period: int = 14,
    entry_price: Decimal | None = None,
) -> Decimal:
    """Стоп по последнему структурному минимуму серии минус ATR-буфер."""
    if series.timeframe not in (Timeframe.H1, Timeframe.D1, Timeframe.M1):
        msg = f"Неподдерживаемый таймфрейм для структурного стопа: {series.timeframe}"
        raise ValueError(msg)

    window = series.tail(lookback) or series.candles
    if not window:
        msg = "Пустая серия: стоп невозможен"
        raise ValueError(msg)

    structural_low = min(c.low for c in window)
    atr = wilder_atr(series, atr_period) if len(series) >= atr_period else ZERO
    entry = entry_price if entry_price is not None else (series.last.close if series.last else ZERO)

    return hard_stop_price(
        entry_price=entry,
        structural_low=min(structural_low, entry * (ONE - MIN_STOP_DISTANCE_PCT)),
        atr=atr,
        atr_multiplier=atr_multiplier,
    )
