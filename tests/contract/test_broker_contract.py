"""Контрактные тесты ``OrderExecutionPort``.

Проверяется поведение, одинаковое для всех реализаций: симулятора бэктеста,
фейка и (при наличии SDK) адаптера T-Invest/sandbox.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from adapters.driven.backtest.simulated_broker import SimulatedBroker
from core.domain.entities import InvalidationRule, Position, ReasoningStep, TradePlan, TradeThesis
from core.domain.enums import OrderStatus, Timeframe, TradePlanStatus, Trend
from tests.fakes import FakeBroker, make_instrument

NOW = __import__("datetime").datetime(
    2026, 1, 10, 10, 0, tzinfo=__import__("datetime").timezone.utc
)


def _plan() -> TradePlan:
    thesis = TradeThesis(
        reasoning_chain=(ReasoningStep(module="t", signal="s", weight=Decimal("1")),),
        confluence_score=Decimal("0.8"),
        timeframe_bias={Timeframe.D1: Trend.UP},
    )
    return TradePlan(
        id=uuid4(),
        instrument=make_instrument(),
        entry_price=Decimal("100"),
        hard_stop_price=Decimal("95"),
        target_price=Decimal("120"),
        thesis=thesis,
        thesis_invalidation=InvalidationRule(
            description="тест", check=lambda s: False, code="noop"
        ),
        max_holding_time=timedelta(hours=72),
        created_at=NOW,
        status=TradePlanStatus.ACTIVE,
        quantity_lots=2,
    )


@pytest.fixture(params=["fake", "simulated"])
def broker(request: pytest.FixtureRequest) -> Any:
    if request.param == "fake":
        return FakeBroker()
    simulated = SimulatedBroker()
    simulated.set_price("uid-sber", Decimal("100"))
    return simulated


async def test_place_order_returns_filled_result(broker: Any) -> None:
    plan = _plan()
    result = await broker.place_order(plan, quantity=2)
    assert result.order_id
    assert result.status is OrderStatus.FILLED
    assert result.filled_lots == 2
    assert result.filled_price is not None
    assert result.filled_price > Decimal("0")


async def test_order_status_after_placement(broker: Any) -> None:
    result = await broker.place_order(_plan(), quantity=1)
    state = await broker.get_order_status(result.order_id)
    assert state.order_id == result.order_id


async def test_cancel_order_does_not_raise(broker: Any) -> None:
    result = await broker.place_order(_plan(), quantity=1)
    await broker.cancel_order(result.order_id)


async def test_close_position_returns_result(broker: Any) -> None:
    plan = _plan()
    await broker.place_order(plan, quantity=2)
    position = Position(
        instrument=plan.instrument,
        quantity=20,
        average_entry=Decimal("100"),
        opened_at=NOW,
        linked_plan_id=plan.id,
    )
    result = await broker.close_position(position, reason="hard_stop")
    assert result.status is OrderStatus.FILLED
    assert result.filled_lots == 2


async def test_open_positions_after_entry(broker: Any) -> None:
    await broker.place_order(_plan(), quantity=3)
    positions = await broker.get_open_positions()
    assert positions, "после исполнения позиция обязана появиться"
    assert all(p.quantity > 0 for p in positions)


async def test_get_instrument_returns_known_uid(broker: Any) -> None:
    instrument = await broker.get_instrument("uid")
    assert instrument is not None
    assert instrument.uid == "uid"


async def test_aclose_is_idempotent(broker: Any) -> None:
    await broker.aclose()
    await broker.aclose()
