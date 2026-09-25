"""Уровни Фибоначчи.

Используются **только** как зоны confluence (дополнительный аргумент), а не как
самостоятельный торговый сигнал. Классический набор уровней коррекции:
23.6%, 38.2%, 50%, 61.8%, 78.6%.

Структура движения: свинг от локального минимума к локальному максимуму.
Уровни строятся «сверху вниз» для восходящего тренда:
``уровень = high − (high − low) × коэффициент``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.analysis.protocols import IndicatorResult
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

#: Классические коэффициенты коррекции.
RETRACEMENT_RATIOS: tuple[Decimal, ...] = (
    Decimal("0.236"),
    Decimal("0.382"),
    Decimal("0.5"),
    Decimal("0.618"),
    Decimal("0.786"),
)

#: «Золотая зона» — наибольший интерес для входа в тренде.
GOLDEN_ZONE = (Decimal("0.5"), Decimal("0.618"))

#: Порог «цена в зоне уровня», в долях от размера свинга.
ZONE_TOLERANCE = Decimal("0.015")


def fibonacci_levels(swing_low: Decimal, swing_high: Decimal) -> dict[str, Decimal]:
    """Рассчитывает уровни коррекции для восходящего свинга."""
    if swing_high <= swing_low:
        msg = f"swing_high ({swing_high}) должен быть больше swing_low ({swing_low})"
        raise ValueError(msg)

    span = swing_high - swing_low
    levels = {
        "swing_low": swing_low,
        "swing_high": swing_high,
    }
    for ratio in RETRACEMENT_RATIOS:
        levels[f"fib_{ratio}"] = swing_high - span * ratio
    return levels


def find_swing(
    series: CandleSeries,
    *,
    lookback: int = 30,
) -> tuple[Decimal, Decimal] | None:
    """Находит последний свинг: минимум и максимум за ``lookback`` свечей."""
    if len(series.candles) < 2:
        return None
    window = series.tail(lookback) or series.candles
    lows = [c.low for c in window]
    highs = [c.high for c in window]
    return min(lows), max(highs)


@dataclass(frozen=True, slots=True, kw_only=True)
class FibProximity:
    """Ближайший уровень Фибоначчи и расстояние до него."""

    level_name: str
    level_price: Decimal
    distance_pct: Decimal
    in_golden_zone: bool


def nearest_fib_level(price: Decimal, levels: dict[str, Decimal]) -> FibProximity | None:
    """Ближайший уровень к цене."""
    candidates = {name: value for name, value in levels.items() if name.startswith("fib_")}
    if not candidates:
        return None

    name, level_price = min(candidates.items(), key=lambda item: abs(item[1] - price))
    distance = abs(level_price - price) / price if price != ZERO else ZERO
    return FibProximity(
        level_name=name,
        level_price=level_price,
        distance_pct=distance,
        in_golden_zone=any(name.endswith(str(ratio)) for ratio in GOLDEN_ZONE),
    )


def is_near_level(
    proximity: FibProximity | None,
    *,
    tolerance: Decimal = ZONE_TOLERANCE,
) -> bool:
    """Попали ли мы в зону уровня с учётом допуска."""
    return proximity is not None and proximity.distance_pct <= tolerance


class FibonacciIndicator:
    """Индикатор Фибоначчи в контракте ``Indicator``."""

    name = "fibonacci"

    __slots__ = ("lookback", "timeframe")

    def __init__(self, lookback: int = 30, *, timeframe: Timeframe = Timeframe.H1) -> None:
        self.lookback = lookback
        self.timeframe = timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        swing = find_swing(series, lookback=self.lookback)
        if swing is None:
            return IndicatorResult(value=ZERO, signal="no_swing")

        low, high = swing
        levels = fibonacci_levels(low, high)
        price = series.last.close if series.last else ZERO
        proximity = nearest_fib_level(price, levels)
        if proximity is None:
            return IndicatorResult(value=ZERO, signal="no_levels", raw=levels)

        signal = "no_fib_confluence"
        if proximity.in_golden_zone and is_near_level(proximity):
            signal = "in_golden_zone"
        elif is_near_level(proximity):
            signal = "near_fib_level"

        return IndicatorResult(
            value=proximity.distance_pct,
            signal=signal,
            raw={**levels, "distance_pct": proximity.distance_pct},
        )
