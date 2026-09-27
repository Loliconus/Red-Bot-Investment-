"""Сверка планов и корзины: «сирота» не должна ронять торговлю и GUI.

Регресс на инцидент: открытый план со старым FIGI вместо ``instrument_uid``
бросал ``ValueError`` в ``get_open_trade_plans`` — мониторинг позиций падал в
цикле, а дашборд отдавал HTTP 500.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from core.domain.entities import (
    InvalidationRule,
    ReasoningStep,
    TradePlan,
    TradeThesis,
)
from core.domain.enums import Timeframe, TradePlanStatus, Trend
from tests.fakes import make_instrument

_NOW = __import__("datetime").datetime(
    2026, 1, 10, 10, 0, tzinfo=__import__("datetime").timezone.utc
)


def _plan(
    instrument: Any,
    *,
    status: TradePlanStatus = TradePlanStatus.ACTIVE,
    created_at: Any = None,
) -> TradePlan:
    return TradePlan(
        id=uuid4(),
        instrument=instrument,
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("95"),
        target_price=Decimal("120"),
        thesis=TradeThesis(
            reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
            confluence_score=Decimal("0.8"),
            timeframe_bias={Timeframe.D1: Trend.UP},
        ),
        thesis_invalidation=InvalidationRule(
            description="тест", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=72),
        created_at=created_at or _NOW,
        status=status,
        quantity_lots=2,
    )


async def test_orphaned_plan_is_closed_with_explicit_reason(context: Any, repository: Any) -> None:
    from application.use_cases.reconcile_trade_plans import (
        ORPHANED_PLAN_REASON,
        reconcile_orphaned_trade_plans,
    )

    instrument = context.instruments[0]
    await repository.save_instrument(instrument)
    healthy = _plan(instrument)
    orphaned = _plan(make_instrument(uid="BBG004731489", ticker="GMKN"))
    await repository.save_trade_plan(healthy)
    await repository.save_trade_plan(orphaned)

    closed = await reconcile_orphaned_trade_plans(context)

    assert closed == (str(orphaned.id),)
    stored = await repository.get_trade_plan(orphaned.id)
    assert stored is None  # инструмента в корзине нет: план не материализуется
    assert healthy.id in {p.id for p in await repository.get_open_trade_plans()}
    assert ORPHANED_PLAN_REASON


async def test_healthy_plans_are_untouched(context: Any, repository: Any) -> None:
    from application.use_cases.reconcile_trade_plans import reconcile_orphaned_trade_plans

    instrument = context.instruments[0]
    await repository.save_instrument(instrument)
    plan = _plan(instrument)
    await repository.save_trade_plan(plan)

    assert await reconcile_orphaned_trade_plans(context) == ()
    assert (await repository.get_trade_plan(plan.id)).status is TradePlanStatus.ACTIVE


async def test_empty_basket_skips_reconciliation(context: Any, repository: Any) -> None:
    """Пустая корзина — это сбой загрузки, а не повод закрывать позиции."""
    from application.use_cases.reconcile_trade_plans import reconcile_orphaned_trade_plans

    orphaned = _plan(make_instrument(uid="BBG004731489", ticker="GMKN"))
    await repository.save_trade_plan(orphaned)
    context.instruments = []

    assert await reconcile_orphaned_trade_plans(context) == ()
    assert await repository.list_orphaned_trade_plan_ids() == (str(orphaned.id),)


def test_instrument_uid_is_not_figi() -> None:
    from application.use_cases.reconcile_trade_plans import looks_like_instrument_uid

    assert looks_like_instrument_uid("962e2a95-02a9-4171-abd7-aa198dbe643a")
    assert not looks_like_instrument_uid("BBG004731489")
    assert not looks_like_instrument_uid("SBER")


def test_suspicious_instruments_reports_non_uid_entries() -> None:
    from application.use_cases.reconcile_trade_plans import suspicious_instruments

    healthy = make_instrument(uid="962e2a95-02a9-4171-abd7-aa198dbe643a")
    legacy = make_instrument(uid="BBG004731489", ticker="GMKN")

    assert suspicious_instruments([healthy, legacy]) == [legacy]


async def test_monitor_positions_survives_orphaned_plan(context: Any, repository: Any) -> None:
    """Мониторинг позиций переживает план без инструмента в корзине."""
    from application.use_cases.monitor_positions import monitor_positions

    instrument = context.instruments[0]
    await repository.save_instrument(instrument)
    await repository.save_trade_plan(_plan(instrument))
    await repository.save_trade_plan(_plan(make_instrument(uid="BBG004731489", ticker="GMKN")))

    report = await monitor_positions(context)

    assert report.checked == 1


async def test_monitor_keeps_going_when_one_plan_fails(
    context: Any, repository: Any, monkeypatch: Any
) -> None:
    """Сбой разбора одного плана не останавливает мониторинг остальных."""
    import sys

    from application.use_cases.monitor_positions import monitor_positions

    # ``application.use_cases`` реэкспортирует функцию с именем модуля, поэтому
    # берём именно модуль: патчим в нём ``decide_exit``.
    monitor_module = sys.modules["application.use_cases.monitor_positions"]

    instrument = context.instruments[0]
    broken_instrument = make_instrument(uid="uid-broken", ticker="BRK")
    await repository.save_instrument(instrument)
    await repository.save_instrument(broken_instrument)
    broken = _plan(broken_instrument)
    healthy = _plan(instrument)
    await repository.save_trade_plan(broken)
    await repository.save_trade_plan(healthy)

    original = monitor_module.decide_exit

    async def flaky(ctx: Any, plan: Any) -> Any:
        if plan.instrument.uid == "uid-broken":
            msg = "нет данных по инструменту"
            raise RuntimeError(msg)
        return await original(ctx, plan)

    monkeypatch.setattr(monitor_module, "decide_exit", flaky)

    report = await monitor_positions(context)

    assert report.checked == 2
    # Проблемный план остался открытым: закрывать позицию вслепую нельзя.
    assert broken.id in {p.id for p in await repository.get_open_trade_plans()}
