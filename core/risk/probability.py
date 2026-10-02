"""Детерминированный long-only риск-слой; float никогда не достигает сайзинга."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from core.domain.probability import (
    ONE,
    ZERO,
    MarketProbabilities,
    ProbabilityRegime,
    ProbabilityRiskPolicy,
    ResearchInstrument,
    require_decimal,
)

BPS = Decimal("10000")


@dataclass(frozen=True, slots=True, kw_only=True)
class Exposure:
    symbol: str
    sector: str
    notional: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbabilitySizing:
    lots: int = 0
    reason: str = ""
    stop_price: Decimal = ZERO
    target_price: Decimal = ZERO


def floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def size_probability_position(
    *,
    signal: MarketProbabilities,
    instrument: ResearchInstrument,
    entry_price: Decimal,
    equity: Decimal,
    cash: Decimal,
    exposures: Sequence[Exposure],
    correlations: Mapping[tuple[str, str], Decimal],
    policy: ProbabilityRiskPolicy,
    killed: bool = False,
) -> ProbabilitySizing:
    """Одобрение только новых позиций. Цена уже включает adverse slippage."""
    for value in (entry_price, equity):
        require_decimal(value, positive=True)
    require_decimal(cash)
    if signal.symbol != instrument.symbol:
        raise ValueError("Сигнал и инструмент не совпадают")
    reason = ""
    if killed:
        reason = "kill_switch"
    elif instrument.is_benchmark or instrument.symbol == "IMOEX":
        reason = "benchmark_not_tradable"
    elif (
        signal.regime is ProbabilityRegime.PANIC
        or signal.panic_probability >= policy.panic_threshold
    ):
        reason = "panic_regime"
    elif signal.trend <= policy.trend_threshold:
        reason = "weak_trend"
    elif signal.up_given_trend < policy.direction_threshold:
        reason = "no_long_edge"
    elif signal.break_within_h >= policy.max_break_probability:
        reason = "break_risk"
    elif len(exposures) >= policy.max_positions:
        reason = "position_limit"
    if reason:
        return ProbabilitySizing(reason=reason)
    for exposure in exposures:
        left, right = sorted((instrument.symbol, exposure.symbol))
        pair = (left, right)
        correlation = correlations.get(pair)
        if correlation is None:
            return ProbabilitySizing(reason="correlation_unknown")
        require_decimal(correlation)
        if not -ONE <= correlation <= ONE:
            raise ValueError("Корреляция вне [-1, 1]")
        if abs(correlation) >= policy.max_correlation:
            return ProbabilitySizing(reason="correlated_position")

    stop = floor_tick(entry_price - policy.stop_atr * signal.atr, instrument.tick_size)
    if stop <= ZERO or stop >= entry_price:
        return ProbabilitySizing(reason="invalid_stop")
    target = floor_tick(entry_price + policy.take_atr * signal.atr, instrument.tick_size)
    stop_fill = floor_tick(stop * (ONE - policy.slippage_bps / BPS), instrument.tick_size)
    target_fill = floor_tick(target * (ONE - policy.slippage_bps / BPS), instrument.tick_size)
    commission = policy.commission_bps / BPS
    unit_risk = entry_price - stop_fill + (entry_price + stop_fill) * commission
    unit_reward = target_fill - entry_price - (entry_price + target_fill) * commission
    if unit_reward <= ZERO or unit_reward / unit_risk < policy.min_net_reward_risk:
        return ProbabilitySizing(reason="costs_exceed_reward")
    confidence = signal.trend * (2 * signal.up_given_trend - ONE) * (ONE - signal.break_within_h)
    vol_weight = min(ONE, policy.target_bar_volatility / signal.volatility)
    gross = sum((e.notional for e in exposures), ZERO)
    sector = sum((e.notional for e in exposures if e.sector == instrument.sector), ZERO)
    notional = min(
        equity * policy.max_position_weight * confidence * vol_weight,
        equity * policy.max_gross_weight - gross,
        equity * policy.max_sector_weight - sector,
        cash / (ONE + policy.commission_bps / BPS),
    )
    if notional <= ZERO:
        return ProbabilitySizing(reason="exposure_limit")
    lot_notional = entry_price * instrument.lot_size
    notional_lots = int((notional / lot_notional).to_integral_value(rounding=ROUND_FLOOR))
    risk_lots = int(
        (
            equity
            * policy.risk_per_trade
            * (ONE - signal.break_within_h)
            / (unit_risk * instrument.lot_size)
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    lots = min(notional_lots, risk_lots)
    if lots < 1:
        return ProbabilitySizing(reason="below_one_lot")
    return ProbabilitySizing(lots=lots, reason="approved", stop_price=stop, target_price=target)
