"""Главный юзкейс: принять торговое решение по инструменту.

Оркестрация (и только она — бизнес-правила живут в ``core``)::

    собрать MarketSnapshot
      → определить режим (D1/H1)
      → посчитать индикаторы (API + собственный реестр)
      → confluence-скор
      → тайминг входа
      → собрать TradePlan
      → фильтр издержек (цель ≥ costs × multiplier)
      → сайзинг от риск-бюджета
      → сохранить снапшоты

Каждый шаг оставляет след в ``DecisionSnapshot``: этого требует режим
самоанализа — «почему бот решил именно так» обязано быть восстановимо.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from application.events import DecisionRecorded
from core.analysis.orderbook_analysis import OrderbookIndicator
from core.analysis.registry import build_default_registry
from core.domain.entities import Instrument, ReasoningStep, StrategyConfig, TradePlan
from core.domain.enums import DecisionType, Timeframe
from core.domain.value_objects import CandleSeries
from core.journal.snapshots import DecisionSnapshot, MarketSnapshot
from core.ports.persistence import DecisionRecord
from core.risk.cost_model import CostFilterResult, estimate_costs
from core.risk.position_sizing import SizingResult, calculate_position_size
from core.risk.thesis_invalidation import build_default_rules
from core.strategy.entry_timing import EntryTiming, evaluate_entry_timing
from core.strategy.regime_detector import detect_regime
from core.strategy.setup_scanner import SetupSignal, scan_setup
from core.strategy.trade_plan_builder import PlanBuildResult, build_trade_plan

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)

#: Сколько истории запрашивать для каждого таймфрейма.
HISTORY_WINDOWS: dict[Timeframe, timedelta] = {
    Timeframe.D1: timedelta(days=365),
    Timeframe.H1: timedelta(days=30),
    Timeframe.M1: timedelta(days=1),
}

#: Индикаторы, которые берём у API (в ``core`` они намеренно не дублируются).
API_INDICATORS: dict[Timeframe, tuple[str, ...]] = {
    Timeframe.D1: ("sma", "ema"),
    Timeframe.H1: ("rsi", "macd", "bollinger"),
    Timeframe.M1: (),
}

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionOutcome:
    """Результат одного цикла анализа."""

    decision: DecisionType
    market_snapshot: MarketSnapshot
    decision_snapshot: DecisionSnapshot
    plan: TradePlan | None = None
    sizing: SizingResult | None = None
    cost_filter: CostFilterResult | None = None
    timing: EntryTiming | None = None
    reason: str = ""

    @property
    def should_execute(self) -> bool:
        return (
            self.decision is DecisionType.ENTER
            and self.plan is not None
            and self.sizing is not None
            and not self.sizing.is_empty
        )


async def _fetch_series(
    ctx: AppContext,
    instrument: Instrument,
    timeframe: Timeframe,
    now: datetime,
) -> CandleSeries:
    window = HISTORY_WINDOWS[timeframe]
    candles = await ctx.market_data.get_candles(
        instrument,
        timeframe,
        from_=now - window,
        to=now,
    )
    return CandleSeries(timeframe=timeframe, candles=tuple(candles))


async def _fetch_api_indicators(
    ctx: AppContext,
    instrument: Instrument,
    timeframe: Timeframe,
) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for name in API_INDICATORS.get(timeframe, ()):
        try:
            raw = await ctx.market_data.get_api_indicator(
                instrument, name, timeframe, ctx.config_params.get(name, {})
            )
        except Exception:  # noqa: BLE001 — отсутствие индикатора не блокирует решение
            logger.warning("api_indicator_unavailable", indicator=name, timeframe=timeframe.value)
            continue
        for key, value in raw.items():
            if value is not None:
                values[key] = Decimal(str(value))
    return values


async def build_market_snapshot(
    ctx: AppContext,
    instrument: Instrument,
) -> MarketSnapshot:
    """Собирает полный снапшот: свечи, режим, индикаторы, стакан, бенчмарк."""
    now = ctx.clock.now()

    series: dict[Timeframe, CandleSeries] = {}
    for timeframe in (Timeframe.D1, Timeframe.H1, Timeframe.M1):
        series[timeframe] = await _fetch_series(ctx, instrument, timeframe, now)

    snapshot = MarketSnapshot.create(
        instrument_uid=instrument.uid,
        captured_at=now,
        ohlcv={tf: s.last for tf, s in series.items() if s.last is not None},
        candles=series,
    )

    # Режим на дневке и часовике.
    for timeframe in (Timeframe.D1, Timeframe.H1):
        if len(series[timeframe]) >= 14:
            try:
                state = detect_regime(series[timeframe])
            except ValueError:
                continue
            snapshot.market_regime[timeframe] = state.regime
            snapshot.indicators.setdefault(timeframe, {})["atr"] = state.atr_value
            snapshot.indicators[timeframe]["atr_pct"] = state.atr_pct
            snapshot.set_signal("regime", state.regime.value, timeframe=timeframe)

    # Индикаторы из API.
    for timeframe in (Timeframe.D1, Timeframe.H1):
        values = await _fetch_api_indicators(ctx, instrument, timeframe)
        snapshot.indicators.setdefault(timeframe, {}).update(values)

    # Собственные индикаторы.
    benchmark_series: CandleSeries | None = None
    if ctx.benchmark is not None:
        benchmark_series = await _fetch_series(ctx, ctx.benchmark, Timeframe.D1, now)
        snapshot.benchmark_snapshot = MarketSnapshot.create(
            instrument_uid=ctx.benchmark.uid,
            captured_at=now,
            ohlcv={Timeframe.D1: benchmark_series.last} if benchmark_series.last else {},
            candles={Timeframe.D1: benchmark_series},
        )

    orderbook = None
    try:
        orderbook = await ctx.market_data.get_orderbook(
            instrument, depth=ctx.settings.tbank.orderbook_depth
        )
    except Exception:  # noqa: BLE001 — стакан может быть недоступен вне сессии
        logger.debug("orderbook_unavailable", uid=instrument.uid)
    snapshot.orderbook = orderbook

    registry = build_default_registry(
        benchmark_d1=benchmark_series,
        orderbook=orderbook,
    )
    for timeframe, candles in series.items():
        results = registry.calculate_all(candles)
        for name, result in results.items():
            if name == "orderbook":
                continue
            snapshot.indicators.setdefault(timeframe, {}).update(dict(result.raw.items()))
            if result.signal:
                snapshot.set_signal(name, result.signal, timeframe=timeframe)

    if orderbook is not None:
        ob_result = OrderbookIndicator(orderbook).calculate()
        snapshot.indicators.setdefault(Timeframe.M1, {}).update(dict(ob_result.raw))
        snapshot.set_signal("orderbook", ob_result.signal, timeframe=Timeframe.M1)

    return snapshot


async def make_decision(
    ctx: AppContext,
    instrument: Instrument,
) -> DecisionOutcome:
    """Принимает решение по одному инструменту и сохраняет снапшоты."""
    snapshot = await build_market_snapshot(ctx, instrument)
    config = ctx.config
    now = ctx.clock.now()

    signal: SetupSignal = scan_setup(snapshot, config)

    if not signal.actionable:
        return await _hold(
            ctx, snapshot, signal, reason=signal.blocking_reason or "сетап не подтверждён"
        )

    timing = evaluate_entry_timing(snapshot, config, confluence_score=signal.score)
    if timing.should_wait:
        return await _hold(ctx, snapshot, signal, reason=timing.reason, timing=timing)
    if timing.decision == "skip":
        return await _hold(ctx, snapshot, signal, reason=timing.reason, timing=timing)

    series_h1 = snapshot.candles.get(Timeframe.H1)
    atr = snapshot.indicator(Timeframe.H1, "atr") or ZERO

    def score_provider(snap: MarketSnapshot) -> Decimal:
        return scan_setup(snap, config).score

    invalidation = build_default_rules(
        entry_score=signal.score,
        score_provider=score_provider,
        timeframes=(Timeframe.H1,),
    )

    built: PlanBuildResult = build_trade_plan(
        instrument=instrument,
        snapshot=snapshot,
        signal=signal,
        config=config,
        invalidation_rule=invalidation,
        now=now,
        atr=atr,
        max_holding_time=timedelta(hours=config.max_holding_hours),
    )

    if built.plan is None:
        return await _reject(ctx, snapshot, signal, reason=built.rejection_reason or "")

    plan = built.plan
    del series_h1

    # --- фильтр издержек: цель обязана превышать издержки в multiplier раз ---
    spread_pct = snapshot.indicator(Timeframe.M1, "spread_pct") or ZERO
    sizing_preview = _preview_sizing(ctx, plan.entry_price, plan.hard_stop_price, instrument)
    notional = (
        sizing_preview.notional
        if sizing_preview.notional > ZERO
        else _fallback_notional(ctx, plan.entry_price)
    )
    costs = estimate_costs(
        notional=notional,
        commission_rate=config.commission_rate,
        spread_pct=spread_pct,
    )
    cost_filter = CostFilterResult.evaluate(
        expected_return_pct=built.expected_return_pct,
        costs_pct=costs.total_pct.value,
        multiplier=config.min_viable_target_multiplier,
    )

    if not cost_filter.passed:
        return await _reject(
            ctx,
            snapshot,
            signal,
            reason=f"фильтр издержек: {cost_filter.reason}",
            plan=plan,
            cost_filter=cost_filter,
        )

    sizing = calculate_position_size(
        equity=ctx.portfolio.total_value if ctx.portfolio else ZERO,
        risk_pct=config.risk_per_trade_pct,
        entry_price=plan.entry_price,
        stop_price=plan.hard_stop_price,
        instrument=instrument,
        max_position_notional=config.max_position_notional,
    )

    if sizing.is_empty:
        return await _reject(
            ctx,
            snapshot,
            signal,
            reason=f"сайзинг: {sizing.reason}",
            plan=plan,
            cost_filter=cost_filter,
        )

    plan.quantity_lots = sizing.lots

    thought = _render_thought(instrument, signal, built, cost_filter, sizing, timing)
    decision_snapshot = DecisionSnapshot.create(
        market_snapshot_id=snapshot.id,
        trade_plan_id=plan.id,
        decision=DecisionType.ENTER,
        reasoning_chain=signal.factors_to_steps(),
        confluence_score=signal.score,
        risk_check_passed=True,
        risk_check_reason=cost_filter.reason,
        thought_text=thought,
        created_at=now,
    )

    await ctx.repository.save_market_snapshot(snapshot)
    await ctx.repository.save_decision_snapshot(decision_snapshot)
    await ctx.repository.save_trade_plan(plan)
    await ctx.event_bus.publish(DecisionRecorded(DecisionRecord(instrument.uid, decision_snapshot)))

    logger.info(
        "decision_enter",
        uid=instrument.uid,
        score=str(signal.score),
        lots=sizing.lots,
        rr=str(built.risk_reward),
    )

    return DecisionOutcome(
        decision=DecisionType.ENTER,
        market_snapshot=snapshot,
        decision_snapshot=decision_snapshot,
        plan=plan,
        sizing=sizing,
        cost_filter=cost_filter,
        timing=timing,
        reason=thought,
    )


def _preview_sizing(
    ctx: AppContext, entry_price: Decimal, stop_price: Decimal, instrument: Instrument
) -> SizingResult:
    """Прикидка размера — нужна, чтобы оценить издержки на реальном объёме."""
    equity = ctx.portfolio.total_value if ctx.portfolio else ZERO
    return calculate_position_size(
        equity=equity,
        risk_pct=ctx.config.risk_per_trade_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        instrument=instrument,
        max_position_notional=ctx.config.max_position_notional,
    )


def _fallback_notional(ctx: AppContext, entry_price: Decimal) -> Decimal:
    """Если риск-бюджет не покрывает лот, считаем издержки на минимальный лот."""
    return entry_price * Decimal("1")


def _render_thought(
    instrument: Instrument,
    signal: SetupSignal,
    built: PlanBuildResult,
    cost_filter: CostFilterResult,
    sizing: SizingResult,
    timing: EntryTiming,
) -> str:
    return (
        f"{instrument.ticker}: confluence {signal.score:.3f}. "
        f"{signal.summary}. Тайминг: {timing.reason}. "
        f"Вход {built.entry_price:.2f}, стоп {(built.stop_price or ZERO):.2f}, "
        f"цель {(built.target_price or ZERO):.2f} (RR {built.risk_reward:.2f}). "
        f"{cost_filter.reason}. Размер: {sizing.lots} лотов ({sizing.reason})."
    )


async def _hold(
    ctx: AppContext,
    snapshot: MarketSnapshot,
    signal: SetupSignal,
    *,
    reason: str,
    timing: EntryTiming | None = None,
) -> DecisionOutcome:
    now = ctx.clock.now()
    decision_snapshot = DecisionSnapshot.create(
        market_snapshot_id=snapshot.id,
        decision=DecisionType.HOLD,
        reasoning_chain=signal.factors_to_steps(),
        confluence_score=signal.score,
        risk_check_passed=False,
        risk_check_reason=reason,
        thought_text=f"{snapshot.instrument_uid}: {reason}",
        created_at=now,
    )
    await ctx.repository.save_market_snapshot(snapshot)
    await ctx.repository.save_decision_snapshot(decision_snapshot)
    await ctx.event_bus.publish(
        DecisionRecorded(DecisionRecord(snapshot.instrument_uid, decision_snapshot))
    )
    logger.info("decision_hold", uid=snapshot.instrument_uid, reason=reason)
    return DecisionOutcome(
        decision=DecisionType.HOLD,
        market_snapshot=snapshot,
        decision_snapshot=decision_snapshot,
        timing=timing,
        reason=reason,
    )


async def _reject(
    ctx: AppContext,
    snapshot: MarketSnapshot,
    signal: SetupSignal,
    *,
    reason: str,
    plan: TradePlan | None = None,
    cost_filter: CostFilterResult | None = None,
) -> DecisionOutcome:
    now = ctx.clock.now()
    plan_id: UUID | None = None
    if plan is not None:
        plan_id = plan.id
        plan.reject(reason, closed_at=now)
        await ctx.repository.save_trade_plan(plan)

    steps: tuple[ReasoningStep, ...] = (
        *signal.factors_to_steps(),
        ReasoningStep(module="risk", signal="rejected", weight=ZERO, comment=reason),
    )
    decision_snapshot = DecisionSnapshot.create(
        market_snapshot_id=snapshot.id,
        trade_plan_id=plan_id,
        decision=DecisionType.REJECT,
        reasoning_chain=steps,
        confluence_score=signal.score,
        risk_check_passed=False,
        risk_check_reason=reason,
        thought_text=f"{snapshot.instrument_uid}: отклонено — {reason}",
        created_at=now,
    )
    await ctx.repository.save_market_snapshot(snapshot)
    await ctx.repository.save_decision_snapshot(decision_snapshot)
    await ctx.event_bus.publish(
        DecisionRecorded(DecisionRecord(snapshot.instrument_uid, decision_snapshot))
    )
    logger.info("decision_reject", uid=snapshot.instrument_uid, reason=reason)
    return DecisionOutcome(
        decision=DecisionType.REJECT,
        market_snapshot=snapshot,
        decision_snapshot=decision_snapshot,
        plan=plan,
        cost_filter=cost_filter,
        reason=reason,
    )


def config_preview(config: StrategyConfig) -> dict[str, Decimal]:
    """Публичные параметры конфига для GUI."""
    return {
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "confluence_threshold": config.confluence_threshold,
        "commission_rate": config.commission_rate,
        "min_viable_target_multiplier": config.min_viable_target_multiplier,
    }
