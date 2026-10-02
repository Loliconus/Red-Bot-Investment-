"""Чистые DTO исследовательского контура. Не SDK и не разрешение торговать."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal("0")
ONE = Decimal("1")


def require_utc(moment: datetime) -> None:
    offset = moment.utcoffset()
    if moment.tzinfo is None or offset is None or offset.total_seconds():
        raise ValueError("Исследовательские timestamps должны быть timezone-aware UTC")


def require_decimal(value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Финансовые значения должны быть конечными Decimal")
    if positive and value <= ZERO:
        raise ValueError("Значение должно быть положительным")


class ProbabilityRegime(StrEnum):
    TREND = "trend"
    RANGE = "range"
    PANIC = "panic"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchBar:
    symbol: str
    begin: datetime
    end: datetime  # исключительная граница; бар известен только здесь
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO

    def __post_init__(self) -> None:
        require_utc(self.begin)
        require_utc(self.end)
        if not self.symbol or self.end <= self.begin:
            raise ValueError("Пустой тикер или некорректный интервал бара")
        for price in (self.open, self.high, self.low, self.close):
            require_decimal(price, positive=True)
        require_decimal(self.volume)
        if self.volume < ZERO or self.low > min(self.open, self.close):
            raise ValueError("Некорректный OHLCV")
        if self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Некорректный OHLCV")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchInstrument:
    symbol: str
    lot_size: int
    tick_size: Decimal
    sector: str = "unclassified"
    is_benchmark: bool = False

    def __post_init__(self) -> None:
        if not self.symbol or self.lot_size < 1:
            raise ValueError("Тикер и реальная лотность обязательны")
        require_decimal(self.tick_size, positive=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketProbabilities:
    symbol: str
    asof: datetime
    trend: Decimal
    up_given_trend: Decimal
    break_within_h: Decimal
    atr: Decimal
    volatility: Decimal  # доля, на один базовый бар; не annualized
    regime: ProbabilityRegime = ProbabilityRegime.RANGE
    panic_probability: Decimal = ZERO

    def __post_init__(self) -> None:
        require_utc(self.asof)
        for probability in (
            self.trend,
            self.up_given_trend,
            self.break_within_h,
            self.panic_probability,
        ):
            require_decimal(probability)
            if not ZERO <= probability <= ONE:
                raise ValueError("Вероятность должна лежать в [0, 1]")
        require_decimal(self.atr, positive=True)
        require_decimal(self.volatility, positive=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbabilityRiskPolicy:
    trend_threshold: Decimal = Decimal("0.60")
    direction_threshold: Decimal = Decimal("0.58")
    max_break_probability: Decimal = Decimal("0.65")
    panic_threshold: Decimal = Decimal("0.60")
    risk_per_trade: Decimal = Decimal("0.01")
    target_bar_volatility: Decimal = Decimal("0.006")
    max_position_weight: Decimal = Decimal("0.20")
    max_gross_weight: Decimal = Decimal("0.90")
    max_sector_weight: Decimal = Decimal("0.35")
    max_correlation: Decimal = Decimal("0.80")
    max_daily_drawdown: Decimal = Decimal("0.03")
    commission_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    stop_atr: Decimal = Decimal("2")
    take_atr: Decimal = Decimal("3")
    trailing_atr: Decimal = Decimal("2.5")
    min_net_reward_risk: Decimal = Decimal("1")
    max_holding_bars: int = 12
    max_positions: int = 4

    def __post_init__(self) -> None:
        for value in (
            self.trend_threshold,
            self.direction_threshold,
            self.max_break_probability,
            self.panic_threshold,
            self.risk_per_trade,
            self.max_position_weight,
            self.max_gross_weight,
            self.max_sector_weight,
            self.max_correlation,
            self.max_daily_drawdown,
        ):
            require_decimal(value, positive=True)
            if value > ONE:
                raise ValueError("Лимиты задаются долями от единицы")
        if self.direction_threshold <= Decimal("0.5"):
            raise ValueError("Long-only direction threshold должен быть > 0.5")
        for value in (
            self.target_bar_volatility,
            self.stop_atr,
            self.take_atr,
            self.trailing_atr,
            self.min_net_reward_risk,
        ):
            require_decimal(value, positive=True)
        for value in (self.commission_bps, self.slippage_bps):
            require_decimal(value)
            if not ZERO <= value <= Decimal("500"):
                raise ValueError("Издержки должны быть в [0, 500] bps на сторону")
        if self.max_holding_bars < 1 or self.max_positions < 1:
            raise ValueError("Некорректный лимит количества / времени")
