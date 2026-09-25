"""Мониторинг открытых позиций: три обязательных механизма выхода.

Порядок проверок принципиален:

1. **hard stop** — цена дошла до железного стопа;
2. **инвалидация тезиса** — причины держать позицию больше нет;
3. **time exit** — истёк TTL идеи;
4. **тейк** — цель достигнута.

Hard stop проверяется первым: это защита капитала, она важнее анализа.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from core.domain.entities import TradePlan
from core.domain.enums import ExitReason, Timeframe, TradePlanStatus
from core.domain.events import (
    HardStopTriggered,
    PositionClosed,
    TargetReached,
    ThesisInvalidated,
    TimeExitTriggered,
)
from core.domain.value_objects import CandleSeries
from core.journal.snapshots import MarketSnapshot
from core.risk.hard_stop import is_hard_stop_triggered
from core.risk.time_exit import check_time_exit
from core.strategy.setup_scanner import scan_setup

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)

ZERO = Decimal("0")

#: Сколько минут истории запрашивать для получения текущей цены.
PRICE_LOOKBACK = timedelta(minutes=5)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitDecision:
    """Решение о выходе из позиции."""

    plan_id: str
    reason: ExitReason | None
    current_price: Decimal
    detail: str

    @property
    def should_exit(self) -> bool:
        return self.reason is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitoringReport:
    checked: int
    exits: tuple[ExitDecision, ...]

    @property
    def exits_count(self) -> int:
        return len(self.exits)


async def current_price(ctx: AppContext, plan: TradePlan) -> Decimal | None:
    """Актуальная цена: последняя минутная свеча, иначе цена из плана."""
    now = ctx.clock.now()
    try:
        candles = await ctx.market_data.get_candles(
            plan.instrument, Timeframe.M1, from_=now - PRICE_LOOKBACK, to=now
        )
    except Exception:  # noqa: BLE001 — вне сессии история недоступна
        return None
    if not candles:
        return None
    return candles[-1].close


def _event_for(reason: ExitReason, plan: TradePlan, price: Decimal, detail: str) -> object:
    now = plan.created_at  # момент считает вызывающий код
    if reason is ExitReason.HARD_STOP:
        return HardStopTriggered(
            occurred_at=now,
            plan_id=plan.id,
            instrument_uid=plan.instrument.uid,
            stop_price=plan.hard_stop_price,
            current_price=price,
        )
    if reason is ExitReason.INVALIDATION:
        return ThesisInvalidated(
            occurred_at=now,
            plan_id=plan.id,
            instrument_uid=plan.instrument.uid,
            rule_code=detail,
            description=detail,
        )
    if reason is ExitReason.TIME_EXIT:
        return TimeExitTriggered(
            occurred_at=now,
            plan_id=plan.id,
            instrument_uid=plan.instrument.uid,
            held_for_seconds=int((now - plan.created_at).total_seconds()),
        )
    return TargetReached(
        occurred_at=now,
        plan_id=plan.id,
        instrument_uid=plan.instrument.uid,
        target_price=plan.target_price,
        exit_price=price,
    )


async def decide_exit(ctx: AppContext, plan: TradePlan) -> ExitDecision:
    """Проверяет один план на необходимость выхода."""
    price = await current_price(ctx, plan)
    if price is None:
        return ExitDecision(
            plan_id=str(plan.id), reason=None, current_price=ZERO, detail="нет цены"
        )

    # 1. Hard stop — приоритет над всем остальным.
    if is_hard_stop_triggered(current_price=price, stop_price=plan.hard_stop_price):
        return ExitDecision(
            plan_id=str(plan.id),
            reason=ExitReason.HARD_STOP,
            current_price=price,
            detail=f"цена {price} <= стопа {plan.hard_stop_price}",
        )

    # 2. Инвалидация тезиса.
    snapshot = await _light_snapshot(ctx, plan, price)
    invalidated = plan.thesis_invalidation.check(snapshot)
    if invalidated:
        return ExitDecision(
            plan_id=str(plan.id),
            reason=ExitReason.INVALIDATION,
            current_price=price,
            detail=plan.thesis_invalidation.description,
        )

    # 3. Time exit (TTL идеи).
    time_result = check_time_exit(plan, ctx.clock.now())
    if time_result.triggered:
        return ExitDecision(
            plan_id=str(plan.id),
            reason=ExitReason.TIME_EXIT,
            current_price=price,
            detail=time_result.reason,
        )

    # 4. Тейк.
    if price >= plan.target_price:
        return ExitDecision(
            plan_id=str(plan.id),
            reason=ExitReason.TARGET,
            current_price=price,
            detail=f"цена {price} достигла цели {plan.target_price}",
        )

    return ExitDecision(plan_id=str(plan.id), reason=None, current_price=price, detail="удержание")


async def _light_snapshot(ctx: AppContext, plan: TradePlan, price: Decimal) -> MarketSnapshot:
    """Лёгкий снапшот для проверки инвалидации: без тяжёлых сетевых вызовов."""
    from core.domain.value_objects import OHLCV

    now = ctx.clock.now()
    series = CandleSeries(
        timeframe=Timeframe.M1,
        candles=(
            OHLCV(
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0,
                timestamp=now,
                timeframe=Timeframe.M1,
            ),
        ),
    )
    snapshot = MarketSnapshot.create(
        instrument_uid=plan.instrument.uid,
        captured_at=now,
        ohlcv={Timeframe.M1: series.last} if series.last else {},
        candles={Timeframe.M1: series},
    )
    if plan.instrument.uid in ctx.regime_cache:
        snapshot.market_regime[Timeframe.H1] = ctx.regime_cache[plan.instrument.uid]
    return snapshot


STATUS_BY_REASON: dict[ExitReason, TradePlanStatus] = {
    ExitReason.HARD_STOP: TradePlanStatus.CLOSED_HARD_STOP,
    ExitReason.INVALIDATION: TradePlanStatus.CLOSED_INVALIDATION,
    ExitReason.TIME_EXIT: TradePlanStatus.CLOSED_TIME_EXIT,
    ExitReason.TARGET: TradePlanStatus.CLOSED_TARGET,
    ExitReason.MANUAL: TradePlanStatus.CLOSED_MANUAL,
}


async def monitor_positions(ctx: AppContext) -> MonitoringReport:
    """Проверяет все активные планы и закрывает те, что требуют выхода."""
    plans = await ctx.repository.get_open_trade_plans()
    active = [p for p in plans if p.status is TradePlanStatus.ACTIVE]
    exits: list[ExitDecision] = []

    for plan in active:
        decision = await decide_exit(ctx, plan)
        if not decision.should_exit or decision.reason is None:
            continue

        exits.append(decision)
        reason = decision.reason

        from application.use_cases.execute_order import close_plan

        result = await close_plan(ctx, plan, reason)
        if result:
            plan.close(STATUS_BY_REASON[reason], closed_at=ctx.clock.now())
            await ctx.repository.save_trade_plan(plan)

            await ctx.event_bus.publish(
                PositionClosed(
                    occurred_at=ctx.clock.now(),
                    plan_id=plan.id,
                    instrument_uid=plan.instrument.uid,
                    exit_price=decision.current_price,
                    realized_pnl=(decision.current_price - plan.entry_price)
                    * Decimal(max(plan.quantity_lots, 0))
                    * Decimal(plan.instrument.lot_size),
                )
            )
            logger.info(
                "position_closed",
                plan_id=str(plan.id),
                reason=reason.value,
                price=str(decision.current_price),
            )

    return MonitoringReport(checked=len(active), exits=tuple(exits))


def refresh_regime_cache(ctx: AppContext, uid: str, regime: object) -> None:
    """Кладёт актуальный режим в кеш контекста (используется мониторингом)."""
    if regime is not None:
        ctx.regime_cache[uid] = regime  # type: ignore[assignment]


async def recheck_confluence(ctx: AppContext, plan: TradePlan) -> Decimal:
    """Актуальный confluence-скор по плану — для GUI и инвалидации."""
    from application.use_cases.make_decision import build_market_snapshot

    snapshot = await build_market_snapshot(ctx, plan.instrument)
    return scan_setup(snapshot, ctx.config).score
