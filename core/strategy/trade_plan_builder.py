"""Сборка ``TradePlan`` из подтверждённого сетапа.

Именно здесь_signal превращается в сущность с жизненным циклом: фиксируется
вход, «железный» стоп, цель, TTL идеи и набор правил инвалидации тезиса.

Стоп всегда считается от структуры (локальный минимум) с ATR-буфером, а не
«процент от входа»: так стоп переживает обычный рыночный шум, но срабатывает
при настоящем пробое.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from core.analysis.confluence_scorer import build_thesis
from core.analysis.fibonacci import find_swing
from core.domain.entities import Instrument, InvalidationRule, StrategyConfig, TradePlan
from core.domain.enums import Timeframe
from core.journal.snapshots import MarketSnapshot
from core.risk.hard_stop import hard_stop_price
from core.strategy.setup_scanner import SetupSignal

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")

#: Минимальное отношение прибыли к риску. Ниже не имеет смысла входить.
MIN_RISK_REWARD = Decimal("2")
#: Множитель ATR для буфера под структуру.
ATR_BUFFER_MULTIPLIER = Decimal("1.5")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanBuildResult:
    """Результат сборки плана."""

    plan: TradePlan | None
    rejection_reason: str | None
    entry_price: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    risk_reward: Decimal
    expected_return_pct: Decimal

    @property
    def is_built(self) -> bool:
        return self.plan is not None


def build_trade_plan(
    *,
    instrument: Instrument,
    snapshot: MarketSnapshot,
    signal: SetupSignal,
    config: StrategyConfig,
    invalidation_rule: InvalidationRule,
    now: datetime,
    atr: Decimal,
    min_risk_reward: Decimal = MIN_RISK_REWARD,
    max_holding_time: timedelta | None = None,
) -> PlanBuildResult:
    """Собирает торговый план.

    Возвращает ``plan=None`` с причиной, если:
    * нет цены или структуры для стопа;
    * риск-реворд ниже минимального;
    * сетап не прошёл проверку ``actionable``.
    """
    if not signal.actionable:
        return PlanBuildResult(
            plan=None,
            rejection_reason=signal.blocking_reason or "сетап не подтверждён",
            entry_price=ZERO,
            stop_price=None,
            target_price=None,
            risk_reward=ZERO,
            expected_return_pct=ZERO,
        )

    last = snapshot.ohlcv.get(Timeframe.H1) or snapshot.ohlcv.get(Timeframe.M1)
    if last is None:
        return PlanBuildResult(
            plan=None,
            rejection_reason="нет цены для расчёта входа",
            entry_price=ZERO,
            stop_price=None,
            target_price=None,
            risk_reward=ZERO,
            expected_return_pct=ZERO,
        )

    entry_price = last.close

    series_h1 = snapshot.candles.get(Timeframe.H1)
    swing = find_swing(series_h1, lookback=30) if series_h1 else None
    structural_low = (
        swing[0]
        if swing
        else min(
            (snapshot.ohlcv[tf].low for tf in (Timeframe.H1, Timeframe.M1) if tf in snapshot.ohlcv),
            default=entry_price * (ONE - Decimal("0.02")),
        )
    )

    if structural_low >= entry_price:
        structural_low = entry_price * (ONE - Decimal("0.02"))

    stop_price = hard_stop_price(
        entry_price=entry_price,
        structural_low=structural_low,
        atr=atr,
        atr_multiplier=ATR_BUFFER_MULTIPLIER,
    )

    risk = entry_price - stop_price
    if risk <= ZERO:
        return PlanBuildResult(
            plan=None,
            rejection_reason="не удалось построить корректный стоп",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=None,
            risk_reward=ZERO,
            expected_return_pct=ZERO,
        )

    # Цель: максимум из «риск × RR» и ближайшего структурного максимума.
    target_price = entry_price + risk * min_risk_reward
    if swing is not None:
        target_price = max(target_price, swing[1])

    risk_reward = (target_price - entry_price) / risk
    expected_return_pct = (target_price - entry_price) / entry_price

    if risk_reward < MIN_RISK_REWARD:
        return PlanBuildResult(
            plan=None,
            rejection_reason=f"risk/reward {risk_reward:.2f} ниже минимума {min_risk_reward}",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            risk_reward=risk_reward,
            expected_return_pct=expected_return_pct,
        )

    thesis = build_thesis(
        signal.factors,
        timeframe_bias=signal.bias,
        confluence_score=signal.score,
        summary=signal.summary,
    )

    plan = TradePlan.create(
        instrument=instrument,
        entry_price=entry_price,
        hard_stop_price=stop_price,
        target_price=target_price,
        thesis=thesis,
        thesis_invalidation=invalidation_rule,
        max_holding_time=max_holding_time or timedelta(hours=config.max_holding_hours),
        created_at=now,
    )

    return PlanBuildResult(
        plan=plan,
        rejection_reason=None,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        risk_reward=risk_reward,
        expected_return_pct=expected_return_pct,
    )
