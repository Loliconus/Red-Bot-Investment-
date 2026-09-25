"""Юнит-тесты доменных сущностей и value objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.entities import (
    Instrument,
    InvalidationRule,
    Position,
    ReasoningStep,
    TradePlan,
    TradeThesis,
)
from core.domain.enums import Timeframe, TradePlanStatus, Trend
from core.domain.value_objects import (
    OHLCV,
    Money,
    OrderbookLevel,
    OrderbookSnapshot,
    Percentage,
    Price,
    TimeRange,
)
from core.journal.snapshots import MarketSnapshot

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(uid="uid", ticker="SBER", lot_size=10)


def _thesis() -> TradeThesis:
    return TradeThesis(
        reasoning_chain=(ReasoningStep(module="trend", signal="up", weight=Decimal("0.5")),),
        confluence_score=Decimal("0.7"),
        timeframe_bias={Timeframe.D1: Trend.UP},
    )


def _rule() -> InvalidationRule:
    return InvalidationRule(description="тест", check=lambda s: False, code="test")


def _plan(**overrides: object) -> TradePlan:
    params: dict[str, object] = {
        "instrument": _instrument(),
        "entry_price": Decimal("100"),
        "hard_stop_price": Decimal("95"),
        "target_price": Decimal("110"),
        "thesis": _thesis(),
        "thesis_invalidation": _rule(),
        "max_holding_time": timedelta(hours=72),
        "created_at": NOW,
    }
    params.update(overrides)
    return TradePlan.create(**params)  # type: ignore[arg-type]


# ------------------------------------------------------------------ TradePlan
def test_trade_plan_rejects_stop_above_entry() -> None:
    with pytest.raises(ValueError, match="hard stop"):
        _plan(entry_price=Decimal("100"), hard_stop_price=Decimal("101"))


def test_trade_plan_rejects_target_below_entry() -> None:
    with pytest.raises(ValueError, match="target"):
        _plan(target_price=Decimal("99"))


def test_trade_plan_rejects_too_small_ttl() -> None:
    with pytest.raises(ValueError, match="max_holding_time"):
        _plan(max_holding_time=timedelta(seconds=10))


def test_trade_plan_metrics() -> None:
    plan = _plan()
    assert plan.risk_per_unit == Decimal("5")
    assert plan.reward_per_unit == Decimal("10")
    assert plan.risk_reward_ratio == Decimal("2")
    assert plan.risk_pct == Decimal("0.05")
    assert plan.expected_return_pct == Decimal("0.1")


def test_trade_plan_lifecycle_proposed_pending_active_closed() -> None:
    plan = _plan()
    assert plan.status is TradePlanStatus.PROPOSED
    assert plan.is_open

    plan.mark_pending()
    assert plan.status is TradePlanStatus.PENDING

    plan.activate()
    assert plan.status is TradePlanStatus.ACTIVE

    plan.close(TradePlanStatus.CLOSED_TARGET, closed_at=NOW)
    assert plan.status is TradePlanStatus.CLOSED_TARGET
    assert plan.is_terminal
    assert not plan.is_open


def test_trade_plan_cannot_activate_from_proposed() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="PENDING"):
        plan.activate()


def test_trade_plan_reject_sets_reason() -> None:
    plan = _plan()
    plan.reject("дорого", closed_at=NOW)
    assert plan.status is TradePlanStatus.REJECTED
    assert plan.rejection_reason == "дорого"
    assert plan.closed_at == NOW


def test_trade_plan_expires_at() -> None:
    plan = _plan(max_holding_time=timedelta(hours=72))
    assert plan.expires_at() == NOW + timedelta(hours=72)


# ------------------------------------------------------------------ Instrument
def test_instrument_notional_multiplies_by_lot() -> None:
    instrument = _instrument()  # lot 10
    assert instrument.notional(Decimal("100"), lots=3) == Decimal("3000")
    assert instrument.units_for_lots(3) == 30


def test_instrument_rejects_zero_lot() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        Instrument(uid="u", ticker="T", lot_size=0)


# ------------------------------------------------------------------ Value objects
def test_money_rejects_float() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Money(amount=100.0)  # type: ignore[arg-type]


def test_money_arithmetic_same_currency() -> None:
    left = Money(amount=Decimal("100"))
    right = Money(amount=Decimal("40"))
    assert (left + right).amount == Decimal("140")
    assert (left - right).amount == Decimal("60")
    assert (left * 2).amount == Decimal("200")
    assert left > right


def test_money_rejects_currency_mixing() -> None:
    rub = Money(amount=Decimal("100"))
    usd = Money(amount=Decimal("100"), currency="USD")
    with pytest.raises(ValueError, match="валют"):
        _ = rub + usd


def test_price_rejects_negative() -> None:
    with pytest.raises(ValueError, match="отрицательн"):
        Price(value=Decimal("-1"))


def test_percentage_conversions() -> None:
    pct = Percentage.from_pct_points(Decimal("0.6"))
    assert pct.value == Decimal("0.006")
    assert pct.as_pct_points() == Decimal("0.6")
    assert pct.of(Decimal("1000")) == Decimal("6")


def test_time_range_contains_half_open() -> None:
    start = NOW
    end = NOW + timedelta(hours=1)
    window = TimeRange(start=start, end=end)
    assert window.contains(start)
    assert not window.contains(end)


def test_time_range_rejects_inverted() -> None:
    with pytest.raises(ValueError):
        TimeRange(start=NOW, end=NOW - timedelta(hours=1))


def test_ohlcv_rejects_high_below_low() -> None:
    with pytest.raises(ValueError, match="high"):
        OHLCV(
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("11"),
            close=Decimal("10"),
            volume=1,
            timestamp=NOW,
            timeframe=Timeframe.D1,
        )


def test_orderbook_metrics() -> None:
    book = OrderbookSnapshot(
        bids=(OrderbookLevel(price=Decimal("99"), quantity=700),),
        asks=(OrderbookLevel(price=Decimal("101"), quantity=300),),
        captured_at=NOW,
    )
    assert book.spread == Decimal("2")
    assert book.mid_price == Decimal("100")
    assert book.spread_pct == Decimal("0.02")
    assert book.imbalance == Decimal("0.4")


def test_empty_orderbook_is_safe() -> None:
    empty = OrderbookSnapshot(bids=(), asks=(), captured_at=NOW)
    assert empty.spread == Decimal("0")
    assert empty.mid_price == Decimal("0")
    assert empty.imbalance == Decimal("0")


# ------------------------------------------------------------------ Position
def test_position_lots_and_pnl() -> None:
    instrument = _instrument()
    position = Position(
        instrument=instrument,
        quantity=100,
        average_entry=Decimal("100"),
        opened_at=NOW,
        linked_plan_id=_plan().id,
    )
    assert position.lots == 10
    assert position.market_value(Decimal("110")) == Decimal("11000")
    assert position.unrealized_pnl_pct(Decimal("110")) == Decimal("0.1")
    assert position.unrealized_pnl(Decimal("110")) == Decimal("1000")


def test_position_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="положительн"):
        Position(
            instrument=_instrument(),
            quantity=0,
            average_entry=Decimal("100"),
            opened_at=NOW,
            linked_plan_id=_plan().id,
        )


# ------------------------------------------------------------------ Snapshot
def test_market_snapshot_requires_tz_aware_time() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        MarketSnapshot.create(
            instrument_uid="uid",
            captured_at=datetime(2026, 1, 10, 10, 0),  # naive
        )


def test_market_snapshot_signals() -> None:
    snapshot = MarketSnapshot.create(instrument_uid="uid", captured_at=NOW)
    snapshot.set_signal("fibonacci", "in_golden_zone", timeframe=Timeframe.H1)
    assert snapshot.signal_of("fibonacci", Timeframe.H1) == "in_golden_zone"
    assert snapshot.signal_of("fibonacci", Timeframe.D1) == ""
    assert snapshot.signal_of("fibonacci") == "in_golden_zone"
