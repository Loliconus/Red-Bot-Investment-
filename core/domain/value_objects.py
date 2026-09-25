"""Value objects домена.

Соглашения:
* все VO — ``frozen=True, slots=True, kw_only=True`` (иммутабельность,
  экономия памяти, защита от перепутанных позиционных аргументов);
* деньги и цены — только ``Decimal``;
* ``*_at`` — ``datetime`` момента события;
* ``*_pct`` — ``Decimal``-доля от единицы (0.01 == 1%, не 1.0);
* ``*_id`` — ``UUID``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Self

from core.domain.enums import Timeframe

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True, kw_only=True)
class Money:
    """Денежная величина в явной валюте."""

    amount: Decimal
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            msg = f"Money.amount должен быть Decimal, получен {type(self.amount).__name__}"
            raise TypeError(msg)

    def __add__(self, other: Money) -> Money:
        _assert_same_currency(self, other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        _assert_same_currency(self, other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money(amount=self.amount * Decimal(factor), currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        _assert_same_currency(self, other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        _assert_same_currency(self, other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        _assert_same_currency(self, other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        _assert_same_currency(self, other)
        return self.amount >= other.amount


def _assert_same_currency(left: Money, right: Money) -> None:
    if left.currency != right.currency:
        msg = f"Разные валюты: {left.currency} и {right.currency}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Price:
    """Ценовой уровень. Не валюта — отдельный тип, чтобы не путать с Money."""

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            msg = f"Price.value должен быть Decimal, получен {type(self.value).__name__}"
            raise TypeError(msg)
        if self.value < ZERO:
            msg = f"Цена не может быть отрицательной: {self.value}"
            raise ValueError(msg)

    def distance_pct(self, other: Price) -> Decimal:
        """Расстояние до другой цены в долях от текущей."""
        if self.value == ZERO:
            return ZERO
        return (other.value - self.value) / self.value

    def __lt__(self, other: Price) -> bool:
        return self.value < other.value

    def __le__(self, other: Price) -> bool:
        return self.value <= other.value

    def __gt__(self, other: Price) -> bool:
        return self.value > other.value

    def __ge__(self, other: Price) -> bool:
        return self.value >= other.value


@dataclass(frozen=True, slots=True, kw_only=True)
class Percentage:
    """Доля от единицы. 0.006 == 0.6%. Хранить как «0.6» запрещено."""

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            msg = f"Percentage.value должен быть Decimal, получен {type(self.value).__name__}"
            raise TypeError(msg)

    @classmethod
    def from_pct_points(cls, points: Decimal) -> Self:
        """Из «процентных пунктов»: 0.6 -> 0.006."""
        return cls(value=Decimal(points) / HUNDRED)

    def as_pct_points(self) -> Decimal:
        return self.value * HUNDRED

    def of(self, base: Decimal) -> Decimal:
        return base * self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeRange:
    """Полуоткрытый интервал [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end < self.start:
            msg = f"end ({self.end}) раньше start ({self.start})"
            raise ValueError(msg)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass(frozen=True, slots=True, kw_only=True)
class Ticker:
    """Биржевой тикер с класс-кодом. Тикер сам по себе не уникален."""

    symbol: str
    class_code: str

    def __str__(self) -> str:
        return f"{self.symbol}_{self.class_code}"


@dataclass(frozen=True, slots=True, kw_only=True)
class OHLCV:
    """Одна свеча. Цены — за одну ценную бумагу, не за лот."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: datetime
    timeframe: Timeframe

    def __post_init__(self) -> None:
        if self.high < self.low:
            msg = f"high ({self.high}) меньше low ({self.low})"
            raise ValueError(msg)

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")

    @property
    def range(self) -> Decimal:
        return self.high - self.low


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderbookLevel:
    price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderbookSnapshot:
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    captured_at: datetime

    @property
    def spread(self) -> Decimal:
        if not self.bids or not self.asks:
            return ZERO
        return self.asks[0].price - self.bids[0].price

    @property
    def mid_price(self) -> Decimal:
        if not self.bids or not self.asks:
            return ZERO
        return (self.asks[0].price + self.bids[0].price) / Decimal("2")

    @property
    def spread_pct(self) -> Decimal:
        mid = self.mid_price
        if mid == ZERO:
            return ZERO
        return self.spread / mid

    @property
    def imbalance(self) -> Decimal:
        """Дисбаланс объёма в диапазоне [-1, 1]. >0 — перевес покупателей."""
        bid_vol = sum(level.quantity for level in self.bids)
        ask_vol = sum(level.quantity for level in self.asks)
        total = bid_vol + ask_vol
        if total == 0:
            return ZERO
        return Decimal(bid_vol - ask_vol) / Decimal(total)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandleSeries:
    """Именованная последовательность свечей одного инструмента и таймфрейма."""

    timeframe: Timeframe
    candles: tuple[OHLCV, ...]

    def __post_init__(self) -> None:
        if any(c.timeframe is not self.timeframe for c in self.candles):
            msg = "Все свечи серии обязаны иметь один таймфрейм"
            raise ValueError(msg)

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def last(self) -> OHLCV | None:
        return self.candles[-1] if self.candles else None

    def closes(self) -> tuple[Decimal, ...]:
        return tuple(c.close for c in self.candles)

    def tail(self, count: int) -> tuple[OHLCV, ...]:
        return self.candles[-count:] if count > 0 else ()
