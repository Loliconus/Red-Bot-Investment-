"""Юнит-тесты риск-модуля."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.entities import Instrument, InvalidationRule
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries
from core.journal.snapshots import MarketSnapshot
from core.risk.cost_model import (
    CostFilterResult,
    estimate_costs,
    is_target_viable,
    min_viable_target_pct,
    round_trip_commission_pct,
    viability_margin,
)
from core.risk.hard_stop import (
    check_hard_stop,
    distance_in_atr,
    hard_stop_price,
    is_hard_stop_triggered,
    structural_stop_from_series,
    trailing_stop_price,
)
from core.risk.position_sizing import (
    calculate_position_size,
    risk_per_unit,
    size_from_budget,
)
from core.risk.thesis_invalidation import (
    build_default_rules,
    combine_rules,
    find_invalidated,
    market_driven_rule,
    relative_strength_rule,
)
from core.risk.time_exit import check_time_exit, is_expired, remaining_time

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


def _instrument(lot: int = 10) -> Instrument:
    return Instrument(uid="uid", ticker="SBER", lot_size=lot)


# ------------------------------------------------------------------ издержки
def test_round_trip_commission_is_doubled() -> None:
    assert round_trip_commission_pct(Decimal("0.003")) == Decimal("0.006")


def test_estimate_costs_sums_all_components() -> None:
    costs = estimate_costs(
        notional=Decimal("100000"),
        commission_rate=Decimal("0.003"),
        spread_pct=Decimal("0.001"),
        slippage_pct=Decimal("0.0005"),
    )
    assert costs.commission_pct.value == Decimal("0.006")
    assert costs.total_pct.value == Decimal("0.0075")
    assert costs.commission_amount == Decimal("600")
    assert costs.total_amount == Decimal("750")


def test_estimate_costs_rejects_negative_notional() -> None:
    with pytest.raises(ValueError, match="notional"):
        estimate_costs(notional=Decimal("-1"), commission_rate=Decimal("0.003"))


def test_cost_filter_passes_when_target_is_twice_costs() -> None:
    result = CostFilterResult.evaluate(
        expected_return_pct=Decimal("0.014"),
        costs_pct=Decimal("0.007"),
        multiplier=Decimal("2"),
    )
    assert result.passed
    assert result.actual_multiplier == Decimal("2")


def test_cost_filter_blocks_tiny_target() -> None:
    """Главный сценарий фильтра: цель меньше издержек × 2 → отказ."""
    result = CostFilterResult.evaluate(
        expected_return_pct=Decimal("0.008"),
        costs_pct=Decimal("0.007"),
        multiplier=Decimal("2"),
    )
    assert not result.passed
    assert result.required_pct == min_viable_target_pct(Decimal("0.007"))


def test_is_target_viable_and_margin() -> None:
    assert is_target_viable(Decimal("0.02"), Decimal("0.007"))
    assert not is_target_viable(Decimal("0.01"), Decimal("0.007"))
    assert viability_margin(Decimal("0.021"), Decimal("0.007")) == Decimal("3")


# ------------------------------------------------------------------ сайзинг
def test_position_size_formula() -> None:
    """Size = (Капитал × Риск%) / |Entry − Stop|, с округлением до лотов вниз."""
    result = calculate_position_size(
        equity=Decimal("100000"),
        risk_pct=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        instrument=_instrument(lot=10),
    )
    # бюджет 1000 / риск 5 = 200 штук → 20 лотов
    assert result.units == 200
    assert result.lots == 20
    assert result.notional == Decimal("20000")
    assert result.risk_amount == Decimal("1000")
    assert not result.capped_by_notional


def test_position_size_rounds_down_to_lot() -> None:
    result = calculate_position_size(
        equity=Decimal("100000"),
        risk_pct=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_price=Decimal("94.9"),
        instrument=_instantiate_lot_10(),
    )
    # 1000 / 5.1 = 196.07 → 196 штук → 19 лотов → 190 штук
    assert result.lots == 19
    assert result.units == 190


def _instantiate_lot_10() -> Instrument:
    return Instrument(uid="uid", ticker="SBER", lot_size=10)


def test_position_size_respects_notional_cap() -> None:
    result = calculate_position_size(
        equity=Decimal("10000000"),
        risk_pct=Decimal("0.01"),
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        instrument=_instrument(lot=10),
        max_position_notional=Decimal("50000"),
    )
    assert result.capped_by_notional
    assert result.notional <= Decimal("50000")


def test_position_size_zero_when_budget_below_one_lot() -> None:
    result = calculate_position_size(
        equity=Decimal("100"),
        risk_pct=Decimal("0.01"),
        entry_price=Decimal("1000"),
        stop_price=Decimal("990"),
        instrument=_instrument(lot=10),
    )
    assert result.is_empty
    assert result.lots == 0


def test_position_size_rejects_stop_above_entry() -> None:
    with pytest.raises(ValueError, match="Стоп"):
        risk_per_unit(Decimal("100"), Decimal("101"))


def test_position_size_validates_risk_pct_range() -> None:
    with pytest.raises(ValueError, match="risk_pct"):
        calculate_position_size(
            equity=Decimal("100000"),
            risk_pct=Decimal("1.5"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            instrument=_instrument(),
        )


def test_size_from_budget_matches_formula() -> None:
    assert size_from_budget(
        equity=Decimal("100000"),
        risk_pct=Decimal("0.02"),
        entry_price=Decimal("100"),
        stop_price=Decimal("90"),
    ) == Decimal("200")


# ------------------------------------------------------------------ hard stop
def test_hard_stop_below_structure() -> None:
    stop = hard_stop_price(
        entry_price=Decimal("100"),
        structural_low=Decimal("95"),
        atr=Decimal("2"),
        atr_multiplier=Decimal("1.5"),
    )
    assert stop == Decimal("92")


def test_hard_stop_never_closer_than_min_distance() -> None:
    stop = hard_stop_price(
        entry_price=Decimal("100"),
        structural_low=Decimal("99.9"),
        atr=Decimal("0"),
    )
    assert stop <= Decimal("99.5")
    assert stop < Decimal("100")


def test_hard_stop_rejects_structure_above_entry() -> None:
    with pytest.raises(ValueError, match="Структурный минимум"):
        hard_stop_price(entry_price=Decimal("100"), structural_low=Decimal("101"), atr=Decimal("1"))


def test_hard_stop_trigger_logic() -> None:
    assert is_hard_stop_triggered(current_price=Decimal("94"), stop_price=Decimal("95"))
    assert not is_hard_stop_triggered(current_price=Decimal("96"), stop_price=Decimal("95"))


def test_trailing_stop_never_moves_down() -> None:
    current = Decimal("90")
    higher = Decimal("100")
    new = trailing_stop_price(
        current_stop=current,
        highest_price_since_entry=higher,
        atr=Decimal("2"),
        atr_multiplier=Decimal("1.5"),
    )
    assert new == Decimal("97")
    # Цена откатилась — стоп остаётся на достигнутом уровне.
    assert (
        trailing_stop_price(
            current_stop=new,
            highest_price_since_entry=Decimal("95"),
            atr=Decimal("2"),
        )
        == new
    )


def test_distance_in_atr() -> None:
    assert distance_in_atr(
        entry_price=Decimal("100"), stop_price=Decimal("94"), atr=Decimal("3")
    ) == Decimal("2")


def test_structural_stop_from_series() -> None:
    series = CandleSeries(
        timeframe=Timeframe.H1,
        candles=tuple(
            OHLCV(
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("90"),
                close=Decimal("101"),
                volume=100,
                timestamp=NOW + timedelta(hours=i),
                timeframe=Timeframe.H1,
            )
            for i in range(20)
        ),
    )
    stop = structural_stop_from_series(series, lookback=20, entry_price=Decimal("100"))
    # минимум 90, ATR=12 (постоянный диапазон 102-90) → 90 - 12×1.5 = 72
    assert stop == Decimal("72")


def test_check_hard_stop_result() -> None:
    result = check_hard_stop(current_price=Decimal("90"), stop_price=Decimal("95"))
    assert result.triggered
    assert result.stop_price == Decimal("95")


# ------------------------------------------------------------------ time exit
def _plan(created_at: datetime = NOW, ttl_hours: int = 72) -> TradePlanStub:
    return TradePlanStub(created_at=created_at, ttl_hours=ttl_hours)


class TradePlanStub:
    """Минимальный объект с интерфейсом TradePlan для TTL-проверок."""

    def __init__(self, created_at: datetime, ttl_hours: int) -> None:
        self.created_at = created_at
        self.max_holding_time = timedelta(hours=ttl_hours)

    def expires_at(self) -> datetime:
        return self.created_at + self.max_holding_time


def test_time_exit_not_triggered_before_ttl() -> None:
    plan = _plan()
    result = check_time_exit(plan, NOW + timedelta(hours=10))  # type: ignore[arg-type]
    assert not result.triggered
    assert result.remaining == timedelta(hours=62)


def test_time_exit_triggered_after_ttl() -> None:
    plan = _plan()
    result = check_time_exit(plan, NOW + timedelta(hours=73))  # type: ignore[arg-type]
    assert result.triggered
    assert result.held_for == timedelta(hours=73)
    assert "TTL" in result.reason


def test_is_expired_and_remaining() -> None:
    plan = _plan(ttl_hours=1)
    assert not is_expired(plan, NOW)  # type: ignore[arg-type]
    assert is_expired(plan, NOW + timedelta(hours=2))  # type: ignore[arg-type]
    assert remaining_time(plan, NOW + timedelta(minutes=30)) == timedelta(minutes=30)  # type: ignore[arg-type]


# ------------------------------------------------------------------ инвалидация
def _snapshot(**indicators: Decimal) -> MarketSnapshot:
    snapshot = MarketSnapshot.create(instrument_uid="uid", captured_at=NOW)
    snapshot.indicators[Timeframe.D1] = dict(indicators)
    return snapshot


def test_relative_strength_rule_triggers_when_lagging() -> None:
    rule = relative_strength_rule()
    assert rule.check(_snapshot(relative_strength=Decimal("-0.05")))
    assert not rule.check(_snapshot(relative_strength=Decimal("0.05")))


def test_market_driven_rule_requires_corr_and_no_alpha() -> None:
    rule = market_driven_rule()
    assert rule.check(_snapshot(correlation=Decimal("0.95"), relative_strength=Decimal("0")))
    assert not rule.check(_snapshot(correlation=Decimal("0.95"), relative_strength=Decimal("0.02")))
    assert not rule.check(_snapshot(correlation=Decimal("0.1"), relative_strength=Decimal("0")))


def test_combine_rules_triggers_on_any() -> None:
    always = InvalidationRule(description="всегда", check=lambda s: True, code="always")
    never = InvalidationRule(description="никогда", check=lambda s: False, code="never")
    assert combine_rules([never, always]).check(_snapshot())
    assert not combine_rules([never, never]).check(_snapshot())


def test_find_invalidated_returns_first_match() -> None:
    never = InvalidationRule(description="никогда", check=lambda s: False, code="never")
    hit = InvalidationRule(description="попал", check=lambda s: True, code="hit")
    assert find_invalidated([never, hit], _snapshot()) is hit
    assert find_invalidated([never], _snapshot()) is None


def test_build_default_rules_uses_score_provider() -> None:
    rules = build_default_rules(
        entry_score=Decimal("0.8"),
        score_provider=lambda s: Decimal("0.1"),
        timeframes=(),
    )
    assert rules.check(_snapshot())
    assert rules.code == "combined"
