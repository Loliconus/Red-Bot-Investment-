"""Сквозной бэктест-прогон на исторических данных.

Смысл теста: стратегия на реплее истории обязана пройти весь путь
«снапшот → confluence → план → фильтр издержек → сайзинг → ордер» и не
упасть. Бэктест использует **тот же** код принятия решения, что и бой, —
иначе сравнение «бэктест против реала» теряет смысл.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
from adapters.driven.backtest.simulated_broker import SimulatedBroker
from application.composition import AppContext
from application.events import EventBus
from application.kill_switch import KillSwitch
from application.use_cases.execute_order import execute_plan
from application.use_cases.make_decision import make_decision
from application.use_cases.monitor_positions import monitor_positions
from config.settings import Settings
from core.domain.entities import (
    InvalidationRule,
    PortfolioState,
    ReasoningStep,
    TradePlan,
    TradeThesis,
)
from core.domain.enums import DecisionType, Timeframe, TradePlanStatus, Trend
from core.ports.clock import FrozenClock
from tests.fakes import (
    InMemoryRepository,
    make_candles,
    make_config,
    make_instrument,
    make_orderbook,
)

NOW = __import__("datetime").datetime(
    2026, 1, 10, 10, 0, tzinfo=__import__("datetime").timezone.utc
)


def make_settings() -> Settings:
    """Бэктесту не нужен токен, но конфиг обязан быть валидным."""
    from config.enums import ExecutionMode

    return Settings(
        execution_mode=ExecutionMode.BACKTEST,
        tbank={"api_token": "backtest", "account_id": "backtest"},
    )


def _build_context(tmp_path: Any) -> AppContext:
    instrument = make_instrument()
    clock = FrozenClock(NOW)
    event_bus = EventBus()

    replay = BacktestReplayAdapter(data_dir=tmp_path / "history", orderbook=make_orderbook())
    for timeframe, count, step, delta in (
        (Timeframe.D1, 200, Decimal("0.4"), timedelta(days=1)),
        (Timeframe.H1, 200, Decimal("0.3"), timedelta(hours=1)),
        (Timeframe.M1, 200, Decimal("0.0001"), timedelta(minutes=1)),
    ):
        candles = make_candles(
            start=NOW - delta * (count - 1),
            count=count,
            timeframe=timeframe,
            base_price=Decimal("135") if timeframe is Timeframe.M1 else Decimal("100"),
            step=step,
        )
        replay.load_from_rows(
            instrument.uid,
            timeframe,
            [(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        )

    broker = SimulatedBroker()
    broker.set_price(instrument.uid, Decimal("135"))

    return AppContext(
        settings=make_settings(),
        market_data=replay,
        broker=broker,
        repository=InMemoryRepository(),
        archive=None,
        clock=clock,
        notifier=None,
        event_bus=event_bus,
        config=make_config(),
        instruments=[instrument],
        benchmark=None,
        portfolio=PortfolioState(
            account_id="backtest",
            total_value=Decimal("1000000"),
            available_cash=Decimal("1000000"),
            positions_value=Decimal("0"),
            updated_at=NOW,
        ),
        kill_switch=KillSwitch(clock=clock, event_bus=event_bus),
        started_at=NOW,
    )


async def test_replay_pipeline_produces_decision(tmp_path: Any) -> None:
    context = _build_context(tmp_path)
    outcome = await make_decision(context, context.instruments[0])

    assert outcome.decision in {DecisionType.ENTER, DecisionType.HOLD, DecisionType.REJECT}
    assert outcome.market_snapshot.indicators
    assert outcome.decision_snapshot.thought_text


async def test_replay_pipeline_can_execute_and_close(tmp_path: Any) -> None:
    context = _build_context(tmp_path)
    instrument = context.instruments[0]

    outcome = await make_decision(context, instrument)
    if not outcome.should_execute:
        pytest.skip("на тестовых данных сетап не прошёл порог — исполнение не проверяем")

    assert outcome.plan is not None and outcome.sizing is not None
    result = await execute_plan(context, outcome.plan, outcome.sizing)
    assert result.filled_lots == outcome.sizing.lots
    assert outcome.plan.status is TradePlanStatus.ACTIVE

    # Позиция открыта → мониторинг видит её.
    report = await monitor_positions(context)
    assert report.checked == 1


async def test_monitor_closes_by_hard_stop(tmp_path: Any) -> None:
    """Стоп срабатывает даже при живом тезисе — это защита капитала."""
    context = _build_context(tmp_path)
    instrument = context.instruments[0]

    thesis = TradeThesis(
        reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.9"),
        timeframe_bias={Timeframe.D1: Trend.UP},
    )
    plan = TradePlan(
        id=uuid4(),
        instrument=instrument,
        entry_price=Decimal("135"),
        hard_stop_price=Decimal("130"),
        target_price=Decimal("150"),
        thesis=thesis,
        thesis_invalidation=InvalidationRule(
            description="никогда", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=72),
        created_at=NOW,
        status=TradePlanStatus.ACTIVE,
        quantity_lots=2,
    )
    await context.repository.save_trade_plan(plan)

    # Цена пробила стоп.
    from core.domain.value_objects import OHLCV

    context.market_data.set_candles(
        instrument.uid,
        Timeframe.M1,
        (
            OHLCV(
                open=Decimal("125"),
                high=Decimal("126"),
                low=Decimal("124"),
                close=Decimal("125"),
                volume=10,
                timestamp=NOW,
                timeframe=Timeframe.M1,
            ),
        ),
    )

    report = await monitor_positions(context)
    assert report.exits_count == 1
    assert plan.status is TradePlanStatus.CLOSED_HARD_STOP


async def test_time_exit_closes_stale_idea(tmp_path: Any) -> None:
    context = _build_context(tmp_path)
    instrument = context.instruments[0]

    thesis = TradeThesis(
        reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.9"),
        timeframe_bias={Timeframe.D1: Trend.UP},
    )
    plan = TradePlan(
        id=uuid4(),
        instrument=instrument,
        entry_price=Decimal("135"),
        hard_stop_price=Decimal("130"),
        target_price=Decimal("150"),
        thesis=thesis,
        thesis_invalidation=InvalidationRule(
            description="никогда", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=1),
        created_at=NOW - timedelta(hours=5),
        status=TradePlanStatus.ACTIVE,
        quantity_lots=1,
    )
    await context.repository.save_trade_plan(plan)

    report = await monitor_positions(context)
    assert report.exits_count == 1
    assert plan.status is TradePlanStatus.CLOSED_TIME_EXIT
