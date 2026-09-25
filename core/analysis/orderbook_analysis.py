"""Анализ биржевого стакана.

Полная тиковая запись истории стакана в MVP исключена (объём данных
несоизмерим с выгодой). Анализируются **снапшоты на момент принятия решения**:
дисбаланс объёмов, спред, плотные уровни (стенки).
"""

from __future__ import annotations

from decimal import Decimal

from core.analysis.protocols import IndicatorResult
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries, OrderbookLevel, OrderbookSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")

#: Дисбаланс, начиная с которого сигнал считается значимым.
IMBALANCE_THRESHOLD = Decimal("0.3")
#: Спред, начиная с которого вход нецелесообразен.
WIDE_SPREAD_PCT = Decimal("0.005")
#: Во сколько раз уровень должен превышать средний, чтобы считаться «стенкой».
WALL_MULTIPLIER = Decimal("3")
#: Доля от стакана, в которой ищем стенки рядом с ценой.
NEAR_ZONE_FRACTION = Decimal("0.5")
#: Доля лучшего уровня: заявка-стенка должна быть крупнее среднего в зоне.
MIN_LEVELS_FOR_ANALYSIS = 2


def levels_average(levels: tuple[OrderbookLevel, ...]) -> Decimal:
    if not levels:
        return ZERO
    total = sum(level.quantity for level in levels)
    return Decimal(total) / Decimal(len(levels))


def find_walls(levels: tuple[OrderbookLevel, ...]) -> tuple[OrderbookLevel, ...]:
    """Плотные уровни — заявки, существенно превышающие средний объём."""
    if len(levels) < MIN_LEVELS_FOR_ANALYSIS:
        return ()
    average = levels_average(levels)
    if average == ZERO:
        return ()
    return tuple(level for level in levels if Decimal(level.quantity) > average * WALL_MULTIPLIER)


def near_walls(
    snapshot: OrderbookSnapshot,
    *,
    side: str,
) -> tuple[OrderbookLevel, ...]:
    """Стенки в ближайшей половине стакана по указанной стороне.

    ``side`` = ``"bid"`` (поддержка) или ``"ask"`` (сопротивление).
    """
    levels = snapshot.bids if side == "bid" else snapshot.asks
    if not levels:
        return ()
    mid = snapshot.mid_price
    if mid == ZERO:
        return ()

    zone = [
        level
        for level in levels
        if abs(level.price - mid) / mid <= NEAR_ZONE_FRACTION * WIDE_SPREAD_PCT * Decimal("10")
    ]
    return find_walls(tuple(zone))


class OrderbookIndicator:
    """Анализ стакана в контракте ``Indicator`` ( value=imbalance )."""

    name = "orderbook"

    __slots__ = ("snapshot", "timeframe")

    def __init__(self, snapshot: OrderbookSnapshot) -> None:
        self.snapshot = snapshot
        self.timeframe = Timeframe.M1

    def calculate(self, series: CandleSeries | None = None) -> IndicatorResult:
        """Серия свечей не нужна — индикатор работает по снапшоту стакана.

        Сигнатура совместима с ``Indicator``: ``series`` опционален.
        """
        del series
        snapshot = self.snapshot
        imbalance = snapshot.imbalance
        spread = snapshot.spread_pct
        bid_walls = near_walls(snapshot, side="bid")
        ask_walls = near_walls(snapshot, side="ask")

        if imbalance > IMBALANCE_THRESHOLD:
            signal = "bid_heavy"
        elif imbalance < -IMBALANCE_THRESHOLD:
            signal = "ask_heavy"
        else:
            signal = "balanced"

        if spread > WIDE_SPREAD_PCT:
            signal = f"{signal}_wide_spread"

        return IndicatorResult(
            value=imbalance,
            signal=signal,
            raw={
                "imbalance": imbalance,
                "spread_pct": spread,
                "bid_walls": Decimal(len(bid_walls)),
                "ask_walls": Decimal(len(ask_walls)),
            },
        )


def orderbook_penalty(spread_pct: Decimal) -> Decimal:
    """Штраф к confluence-скору за широкий спред: 0 при узком, до −1 при широком."""
    if spread_pct <= ZERO:
        return ZERO
    ratio = spread_pct / WIDE_SPREAD_PCT
    return -min(ratio, ONE)
