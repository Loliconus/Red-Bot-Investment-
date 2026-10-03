"""Юнит-тесты юзкейсов приложения (на фейках портов)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from application.composition import AppContext
from application.events import EventBus
from application.kill_switch import KillSwitch
from application.use_cases.archive_old_data import archive_old_data
from application.use_cases.execute_order import build_client_order_id, execute_plan
from application.use_cases.make_decision import build_market_snapshot, make_decision
from application.use_cases.monitor_positions import decide_exit, monitor_positions
from application.use_cases.update_strategy_config import update_strategy_config
from core.domain.enums import DecisionType, ExitReason, Timeframe, TradePlanStatus
from core.domain.value_objects import OHLCV
from core.risk.position_sizing import calculate_position_size
from tests.fakes import (
    FakeMarketData,
    make_candles,
    make_config,
    make_instrument,
)


# ------------------------------------------------------------------ решение
async def test_make_decision_enters_on_strong_setup(context: object) -> None:
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.decision is DecisionType.ENTER, outcome.reason
    assert outcome.should_execute
    assert outcome.plan is not None
    assert outcome.sizing is not None
    assert outcome.sizing.lots > 0
    assert outcome.cost_filter is not None
    assert outcome.cost_filter.passed


async def test_make_decision_persists_snapshots(context: object) -> None:
    await make_decision(context, context.instruments[0])
    assert context.repository.market_snapshots
    assert context.repository.decision_snapshots


async def test_make_decision_waits_when_price_far_above_vwap(
    context: object, instrument: object, candles_d1: tuple, candles_h1: tuple
) -> None:
    """Цена улетела от VWAP — момент плохой, вход откладывается."""
    steep_m1 = make_candles(
        start=context.clock.now() - timedelta(minutes=119),
        count=120,
        timeframe=Timeframe.M1,
        base_price=Decimal("100"),
        step=Decimal("0.5"),
    )
    context.market_data = FakeMarketData(
        {
            (instrument.uid, Timeframe.D1): candles_d1,
            (instrument.uid, Timeframe.H1): candles_h1,
            (instrument.uid, Timeframe.M1): steep_m1,
        },
        orderbook=context.market_data._orderbook,
        indicators=context.market_data._indicators,
    )
    outcome = await make_decision(context, instrument)
    assert outcome.decision is DecisionType.HOLD
    assert "VWAP" in outcome.reason


async def test_make_decision_rejects_tiny_target(context: object, instrument: object) -> None:
    """Цель меньше издержек × 2 → фильтр издержек обязан заблокировать вход."""
    context.config = make_config(min_viable_target_multiplier=Decimal("1000"))
    outcome = await make_decision(context, instrument)
    assert outcome.decision is DecisionType.REJECT
    assert "издержек" in outcome.reason


async def test_make_decision_holds_when_confluence_low(context: object) -> None:
    context.config = make_config(confluence_threshold=Decimal("0.99"))
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.decision is DecisionType.HOLD


async def test_build_market_snapshot_fills_regimes_and_indicators(context: object) -> None:
    snapshot = await build_market_snapshot(context, context.instruments[0])
    assert snapshot.market_regime
    assert snapshot.indicators[Timeframe.H1]
    assert snapshot.signals
    assert snapshot.last_price(Timeframe.H1) is not None


async def test_make_decision_survives_unavailable_orderbook(
    context: object, candles_d1: tuple, candles_h1: tuple, candles_m1: tuple
) -> None:
    context.market_data = FakeMarketData(
        {
            (context.instruments[0].uid, Timeframe.D1): candles_d1,
            (context.instruments[0].uid, Timeframe.H1): candles_h1,
            (context.instruments[0].uid, Timeframe.M1): candles_m1,
        },
        raise_on_orderbook=True,
    )
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.decision in {DecisionType.ENTER, DecisionType.HOLD, DecisionType.REJECT}


# ------------------------------------------------------------------ исполнение
async def test_execute_plan_places_order_and_activates(context: object) -> None:
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.plan is not None and outcome.sizing is not None
    result = await execute_plan(context, outcome.plan, outcome.sizing)
    assert result.client_order_id
    assert outcome.plan.status is TradePlanStatus.ACTIVE
    assert context.broker.placed


async def test_execute_plan_rejects_empty_size(context: object) -> None:
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.plan is not None
    empty = calculate_position_size(
        equity=Decimal("10"),
        risk_pct=Decimal("0.01"),
        entry_price=outcome.plan.entry_price,
        stop_price=outcome.plan.hard_stop_price,
        instrument=outcome.plan.instrument,
    )
    assert empty.is_empty
    result = await execute_plan(context, outcome.plan, empty)
    assert result.status.value == "rejected"
    assert outcome.plan.status is TradePlanStatus.REJECTED


async def test_execute_plan_blocked_by_kill_switch(context: object) -> None:
    await context.kill_switch.engage("тест")
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.plan is not None and outcome.sizing is not None
    result = await execute_plan(context, outcome.plan, outcome.sizing)
    assert result.status.value == "rejected"
    assert not context.broker.placed


def test_client_order_id_is_deterministic_and_bounded() -> None:
    """Ключ идемпотентности обязан быть воспроизводим: иначе это не защита."""
    from uuid import uuid4

    plan_id = uuid4()
    plan = _make_plan_stub(plan_id)
    first = build_client_order_id(plan)
    assert first == plan_id.hex[:12]
    assert build_client_order_id(plan) == first

    closed = build_client_order_id(plan, suffix="close")
    assert closed.endswith("-close")
    assert len(closed) <= 36
    assert closed != first


# ------------------------------------------------------------------ мониторинг
def _make_plan_stub(plan_id: object) -> object:
    """План-заготовка: нужен только id для проверки ключа идемпотентности."""

    class _Stub:
        id = plan_id

    return _Stub()


def _plan_for_monitor(context: object, *, entry: str, stop: str) -> object:
    from uuid import uuid4

    from core.domain.entities import InvalidationRule, ReasoningStep, TradeThesis
    from core.domain.enums import Trend

    thesis = TradeThesis(
        reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.8"),
        timeframe_bias={Timeframe.D1: Trend.UP},
    )
    from core.domain.entities import TradePlan

    return TradePlan(
        id=uuid4(),
        instrument=make_instrument(),
        entry_price=Decimal(entry),
        hard_stop_price=Decimal(stop),
        target_price=Decimal(entry) + Decimal("20"),
        thesis=thesis,
        thesis_invalidation=InvalidationRule(
            description="тест", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=72),
        created_at=context.clock.now(),
        status=TradePlanStatus.ACTIVE,
        quantity_lots=1,
    )


async def test_decide_exit_hard_stop_first(context: object, instrument: object) -> None:
    plan = _plan_for_monitor(context, entry="100", stop="95")
    _set_price(context, instrument, Decimal("90"))
    decision = await decide_exit(context, plan)
    assert decision.should_exit
    assert decision.reason is ExitReason.HARD_STOP


async def test_decide_exit_target(context: object, instrument: object) -> None:
    plan = _plan_for_monitor(context, entry="100", stop="95")
    _set_price(context, instrument, Decimal("150"))
    decision = await decide_exit(context, plan)
    assert decision.reason is ExitReason.TARGET


async def test_decide_exit_time_exit(context: object, instrument: object) -> None:
    plan = _plan_for_monitor(context, entry="100", stop="95")
    plan.created_at = context.clock.now() - timedelta(hours=100)
    _set_price(context, instrument, Decimal("105"))
    decision = await decide_exit(context, plan)
    assert decision.reason is ExitReason.TIME_EXIT


async def test_decide_exit_hold_when_nothing_triggered(context: object, instrument: object) -> None:
    plan = _plan_for_monitor(context, entry="100", stop="95")
    _set_price(context, instrument, Decimal("105"))
    decision = await decide_exit(context, plan)
    assert not decision.should_exit


async def test_monitor_positions_closes_and_persists(context: object, instrument: object) -> None:
    plan = _plan_for_monitor(context, entry="100", stop="95")
    await context.repository.save_instrument(instrument)
    await context.repository.save_trade_plan(plan)
    _set_price(context, instrument, Decimal("80"))

    report = await monitor_positions(context)
    assert report.checked == 1
    assert report.exits_count == 1
    assert plan.status is TradePlanStatus.CLOSED_HARD_STOP
    assert context.broker.closed


def _set_price(context: object, instrument: object, price: Decimal) -> None:
    candle = OHLCV(
        open=price,
        high=price,
        low=price,
        close=price,
        volume=10,
        timestamp=context.clock.now(),
        timeframe=Timeframe.M1,
    )
    context.market_data._candles[(instrument.uid, Timeframe.M1)] = (candle,)


# ------------------------------------------------------------------ прочее
async def test_kill_switch_engages_on_daily_loss(clock: object, event_bus: EventBus) -> None:
    switch = KillSwitch(clock=clock, event_bus=event_bus, daily_loss_limit_pct=Decimal("0.03"))
    switch.reset_day(Decimal("100000"))
    assert not switch.is_engaged
    await switch.update_equity(Decimal("95000"))
    assert switch.is_engaged
    assert "убытка" in switch.reason


async def test_kill_switch_release(clock: object, event_bus: EventBus) -> None:
    switch = KillSwitch(clock=clock, event_bus=event_bus)
    await switch.engage("тест")
    switch.release()
    assert not switch.is_engaged


async def test_archive_old_data_reports_usage(context: object) -> None:
    report = await archive_old_data(context)
    assert report.archived_rows == 42
    assert report.usage_by_layer
    assert context.archive.calls


async def test_update_strategy_config_bumps_version(context: object) -> None:
    await context.repository.save_strategy_config(context.config)
    previous = context.config.version
    updated = await update_strategy_config(context, risk_per_trade_pct=Decimal("0.02"))
    assert updated.version == previous + 1
    assert updated.risk_per_trade_pct == Decimal("0.02")
    assert context.config is updated


async def test_update_strategy_config_falls_back_to_context_config(context: object) -> None:
    """Если БД ещё пуста, источником правды служит конфиг из контекста."""
    context.repository.configs.clear()
    updated = await update_strategy_config(context, risk_per_trade_pct=Decimal("0.02"))
    assert updated.version == context.config.version
    assert updated.risk_per_trade_pct == Decimal("0.02")


async def test_scheduler_runs_tasks_and_stops() -> None:
    from application.scheduler import Scheduler

    calls: list[str] = []
    scheduler = Scheduler()

    async def cycle() -> None:
        calls.append("tick")

    scheduler.add_task("test", cycle, interval_seconds=0.01)

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.08)
    scheduler.stop()
    await asyncio.wait_for(task, timeout=2)
    assert calls
    assert scheduler.stats()["test"]["iterations"] >= 1


async def test_event_bus_isolates_handler_errors() -> None:
    bus = EventBus()
    handled: list[str] = []

    async def broken(event: object) -> None:
        msg = "сломан"
        raise RuntimeError(msg)

    async def working(event: object) -> None:
        handled.append("ok")

    bus.subscribe(str, broken)
    bus.subscribe(str, working)
    await bus.publish("событие")
    assert handled == ["ok"]
    assert bus.subscribers_count(str) == 2


async def test_manage_instruments_add_and_remove(context: AppContext) -> None:
    from application.use_cases.manage_instruments import (
        add_instrument,
        list_instrument_views,
        remove_instrument,
    )
    from tests.fakes import make_catalog_entry

    # Параметры инструмента приходят из API: лот и UID — из ответа, не из кода.
    context.market_data.set_catalog(
        [make_catalog_entry(uid="uid-vtbr", ticker="VTBR", name="Банк ВТБ", lot_size=10000)]
    )

    # Добавляем по тикеру
    inst = await add_instrument(context, "VTBR", "TQBR")
    assert inst.ticker == "VTBR"
    assert inst.lot_size == 10000
    assert inst.uid in [i["uid"] for i in await list_instrument_views(context)]

    # Удаляем
    await remove_instrument(context, inst.uid)
    assert inst.uid not in [i["uid"] for i in await list_instrument_views(context)]


async def test_manage_instruments_add_by_company_name(context: AppContext) -> None:
    from application.use_cases.manage_instruments import add_instrument
    from tests.fakes import make_catalog_entry

    context.market_data.set_catalog(
        [make_catalog_entry(uid="uid-gazp", ticker="GAZP", name="Газпром", lot_size=10)]
    )

    inst = await add_instrument(context, "Газпром (GAZP)", "TQBR")
    assert inst.ticker == "GAZP"
    assert inst.lot_size == 10


async def test_manage_instruments_add_by_russian_name(context: AppContext) -> None:
    """Название компании без тикера ищется через каталог/FindInstrument."""
    from application.use_cases.manage_instruments import add_instrument
    from tests.fakes import make_catalog_entry

    context.market_data.set_catalog(
        [make_catalog_entry(uid="uid-chmf", ticker="CHMF", name="Северсталь", lot_size=100)]
    )

    inst = await add_instrument(context, "Северсталь", "TQBR")
    assert inst.ticker == "CHMF"
    assert inst.uid == "uid-chmf"
    assert "search_instruments:Северсталь" in context.market_data.calls


async def test_manage_instruments_prevents_removing_benchmark(context: AppContext) -> None:
    from application.use_cases.bootstrap_database import seed_instrument
    from application.use_cases.manage_instruments import remove_instrument

    imoex = seed_instrument("uid-imoex", "IMOEX", 1, is_benchmark=True)
    await context.repository.save_instrument(imoex)
    context.instruments.append(imoex)

    with pytest.raises(ValueError, match="бенчмарк"):
        await remove_instrument(context, imoex.uid)


async def test_manage_account_sandbox_operations(context: AppContext) -> None:
    from application.use_cases.manage_account import (
        close_sandbox_account,
        create_sandbox_account,
        get_account_overview,
        refresh_portfolio,
        switch_sandbox_account,
        topup_sandbox,
    )
    from config.enums import ExecutionMode

    context.mode = ExecutionMode.SANDBOX
    overview = await get_account_overview(context)
    assert overview["account_id"]
    assert overview["is_sandbox"]

    # Пополнение
    new_bal = await topup_sandbox(context, Decimal("250000"))
    assert new_bal >= Decimal("250000")
    assert context.portfolio is not None
    assert context.portfolio.total_value == new_bal

    # Создание счёта в песочнице
    new_acc = await create_sandbox_account(context, "Второй счёт")
    assert new_acc
    assert context.active_account_id == new_acc

    # Переключение счетов
    await switch_sandbox_account(context, overview["account_id"])
    assert context.active_account_id == overview["account_id"]

    # Закрытие счёта
    await close_sandbox_account(context, new_acc)
    updated_overview = await get_account_overview(context)
    assert new_acc not in [a["id"] for a in updated_overview["accounts"]]

    # Обновление
    p = await refresh_portfolio(context)
    assert p.total_value > Decimal("0")
