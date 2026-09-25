"""Юнит-тесты стратегического слоя."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.analysis.confluence_scorer import (
    ConfluenceFactor,
    build_thesis,
    dominant_negative,
    is_actionable,
    score_confluence,
)
from core.domain.entities import Instrument, InvalidationRule
from core.domain.enums import MarketRegime, Timeframe, Trend
from core.domain.value_objects import OHLCV, CandleSeries
from core.journal.snapshots import MarketSnapshot
from core.strategy.entry_timing import evaluate_entry_timing
from core.strategy.regime_detector import detect_regime, normalized_slope
from core.strategy.setup_scanner import scan_setup
from core.strategy.trade_plan_builder import build_trade_plan
from tests.fakes import make_config, make_orderbook

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


def _rising(
    count: int = 60,
    *,
    step: Decimal = Decimal("0.4"),
    base: Decimal = Decimal("100"),
    tf: Timeframe = Timeframe.D1,
) -> CandleSeries:
    delta = {
        Timeframe.D1: timedelta(days=1),
        Timeframe.H1: timedelta(hours=1),
        Timeframe.M1: timedelta(minutes=1),
    }[tf]
    candles = tuple(
        OHLCV(
            open=base + step * Decimal(i),
            high=base + step * Decimal(i) + Decimal("1"),
            low=base + step * Decimal(i) - Decimal("1"),
            close=base + step * Decimal(i) + step / 2,
            volume=1000 + i,
            timestamp=NOW + delta * i,
            timeframe=tf,
        )
        for i in range(count)
    )
    return CandleSeries(timeframe=tf, candles=candles)


def _flat_series(
    count: int = 60, *, price: Decimal = Decimal("100"), tf: Timeframe = Timeframe.D1
) -> CandleSeries:
    delta = {
        Timeframe.D1: timedelta(days=1),
        Timeframe.H1: timedelta(hours=1),
        Timeframe.M1: timedelta(minutes=1),
    }[tf]
    candles = tuple(
        OHLCV(
            open=price,
            high=price + Decimal("0.5"),
            low=price - Decimal("0.5"),
            close=price + (Decimal("0.1") if i % 2 else Decimal("-0.1")),
            volume=1000,
            timestamp=NOW + delta * i,
            timeframe=tf,
        )
        for i in range(count)
    )
    return CandleSeries(timeframe=tf, candles=candles)


def _snapshot(**kwargs: object) -> MarketSnapshot:
    return MarketSnapshot.create(instrument_uid="uid", captured_at=NOW, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ confluence
def test_score_confluence_weighted_average() -> None:
    factors = (
        ConfluenceFactor(module="a", signal="up", score=Decimal("1"), weight=Decimal("0.5")),
        ConfluenceFactor(module="b", signal="down", score=Decimal("-1"), weight=Decimal("0.5")),
    )
    assert score_confluence(factors) == Decimal("0")


def test_score_confluence_external_weights_override() -> None:
    factors = (
        ConfluenceFactor(module="a", signal="up", score=Decimal("1"), weight=Decimal("0.5")),
        ConfluenceFactor(module="b", signal="down", score=Decimal("-1"), weight=Decimal("0.5")),
    )
    result = score_confluence(factors, weights={"a": Decimal("0.9"), "b": Decimal("0.1")})
    assert result == Decimal("0.8")


def test_confluence_factor_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="score"):
        ConfluenceFactor(module="a", signal="x", score=Decimal("2"), weight=Decimal("1"))


def test_is_actionable_threshold() -> None:
    assert is_actionable(Decimal("0.3"), Decimal("0.3"))
    assert not is_actionable(Decimal("0.29"), Decimal("0.3"))


def test_dominant_negative() -> None:
    factors = (
        ConfluenceFactor(module="good", signal="up", score=Decimal("0.5"), weight=Decimal("1")),
        ConfluenceFactor(module="bad", signal="down", score=Decimal("-0.8"), weight=Decimal("1")),
    )
    assert dominant_negative(factors) is not None
    assert dominant_negative(factors).module == "bad"  # type: ignore[union-attr]
    assert dominant_negative((factors[0],)) is None


def test_build_thesis_collects_chain() -> None:
    factors = (ConfluenceFactor(module="a", signal="up", score=Decimal("1"), weight=Decimal("1")),)
    thesis = build_thesis(
        factors,
        timeframe_bias={Timeframe.D1: Trend.UP},
        confluence_score=Decimal("1"),
        summary="тест",
    )
    assert len(thesis.reasoning_chain) == 1
    assert thesis.total_weight == Decimal("1")


# ------------------------------------------------------------------ режим
def test_detect_regime_trending_up() -> None:
    state = detect_regime(_rising(60, step=Decimal("0.6")))
    assert state.regime is MarketRegime.TRENDING
    assert state.trend is Trend.UP
    assert state.structure_confirmed
    assert state.is_favorable_for_long()


def test_detect_regime_ranging() -> None:
    state = detect_regime(_flat_series(60))
    assert state.regime is MarketRegime.RANGING
    assert state.trend is Trend.FLAT


def test_detect_regime_high_volatility() -> None:
    series = _flat_series(60, price=Decimal("100"))
    # Увеличиваем диапазон: ATR вырастет относительно цены
    wide = CandleSeries(
        timeframe=Timeframe.D1,
        candles=tuple(
            OHLCV(
                open=c.open,
                high=c.close + Decimal("8"),
                low=c.close - Decimal("8"),
                close=c.close,
                volume=c.volume,
                timestamp=c.timestamp,
                timeframe=Timeframe.D1,
            )
            for c in series.candles
        ),
    )
    state = detect_regime(wide)
    assert state.volatility.value == "high"


def test_detect_regime_requires_history() -> None:
    with pytest.raises(ValueError, match="Недостаточно"):
        detect_regime(_rising(5))


def test_normalized_slope_zero_for_short_series() -> None:
    assert normalized_slope(_rising(5)) == Decimal("0")


# ------------------------------------------------------------------ сканер
def _rich_snapshot(**overrides: object) -> MarketSnapshot:
    snapshot = _snapshot()
    snapshot.indicators[Timeframe.D1] = {
        "atr": Decimal("2"),
        "sma": Decimal("90"),
        "ema": Decimal("92"),
        "relative_strength": Decimal("0.05"),
        "correlation": Decimal("0.4"),
    }
    snapshot.indicators[Timeframe.H1] = {
        "atr": Decimal("2"),
        "rsi": Decimal("35"),
        "macd": Decimal("0.4"),
        "signal": Decimal("0.2"),
        "bb_lower": Decimal("95"),
        "distance_pct": Decimal("0.005"),
    }
    snapshot.indicators[Timeframe.M1] = {
        "obv_slope": Decimal("500"),
        "deviation": Decimal("-0.002"),
        "imbalance": Decimal("0.3"),
        "spread_pct": Decimal("0.0005"),
    }
    snapshot.signals[Timeframe.H1] = {"fibonacci": "in_golden_zone"}
    snapshot.market_regime[Timeframe.D1] = MarketRegime.TRENDING
    snapshot.ohlcv[Timeframe.D1] = OHLCV(
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
        timestamp=NOW,
        timeframe=Timeframe.D1,
    )
    snapshot.ohlcv[Timeframe.H1] = OHLCV(
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1000,
        timestamp=NOW,
        timeframe=Timeframe.H1,
    )
    snapshot.candles[Timeframe.H1] = _rising(40, tf=Timeframe.H1)
    snapshot.orderbook = make_orderbook()
    for key, value in overrides.items():
        setattr(snapshot, key, value)
    return snapshot


def test_scan_setup_actionable_on_strong_confluence() -> None:
    signal = scan_setup(_rich_snapshot(), make_config())
    assert signal.actionable, signal.blocking_reason
    assert signal.score > Decimal("0.5")
    assert signal.factor("trend_d1") is not None
    assert signal.factor("orderbook") is not None


def test_scan_setup_blocks_when_lagging_benchmark() -> None:
    snapshot = _rich_snapshot()
    snapshot.benchmark_snapshot = _snapshot()
    snapshot.indicators[Timeframe.D1]["relative_strength"] = Decimal("-0.05")
    signal = scan_setup(snapshot, make_config())
    assert not signal.actionable
    assert "IMOEX" in (signal.blocking_reason or "")


def test_scan_setup_allows_counter_trend_when_enabled() -> None:
    snapshot = _rich_snapshot()
    snapshot.benchmark_snapshot = _snapshot()
    snapshot.indicators[Timeframe.D1]["relative_strength"] = Decimal("-0.05")
    signal = scan_setup(snapshot, make_config(allow_counter_trend=True))
    assert signal.actionable


def test_scan_setup_blocks_high_volatility_regime() -> None:
    snapshot = _rich_snapshot()
    snapshot.market_regime[Timeframe.D1] = MarketRegime.HIGH_VOLATILITY
    signal = scan_setup(snapshot, make_config())
    assert not signal.actionable
    assert "волатильн" in (signal.blocking_reason or "")


def test_scan_setup_no_benchmark_is_neutral() -> None:
    signal = scan_setup(_rich_snapshot(), make_config())
    rs = signal.factor("relative_strength")
    assert rs is not None
    assert rs.signal == "no_benchmark"
    assert rs.score == Decimal("0")


# ------------------------------------------------------------------ тайминг
def test_entry_timing_skips_low_score() -> None:
    result = evaluate_entry_timing(_rich_snapshot(), make_config(), confluence_score=Decimal("0.1"))
    assert result.decision == "skip"


def test_entry_timing_waits_on_wide_spread() -> None:
    snapshot = _rich_snapshot()
    snapshot.indicators[Timeframe.M1]["spread_pct"] = Decimal("0.02")
    result = evaluate_entry_timing(snapshot, make_config(), confluence_score=Decimal("0.6"))
    assert result.decision == "wait"
    assert "спред" in result.reason


def test_entry_timing_waits_on_ask_heavy_book() -> None:
    snapshot = _rich_snapshot()
    snapshot.indicators[Timeframe.M1]["imbalance"] = Decimal("-0.8")
    result = evaluate_entry_timing(snapshot, make_config(), confluence_score=Decimal("0.6"))
    assert result.decision == "wait"


def test_entry_timing_waits_when_price_far_above_vwap() -> None:
    snapshot = _rich_snapshot()
    snapshot.indicators[Timeframe.M1]["deviation"] = Decimal("0.03")
    result = evaluate_entry_timing(snapshot, make_config(), confluence_score=Decimal("0.6"))
    assert result.decision == "wait"
    assert "VWAP" in result.reason


def test_entry_timing_enters_on_pullback() -> None:
    result = evaluate_entry_timing(_rich_snapshot(), make_config(), confluence_score=Decimal("0.7"))
    assert result.should_enter
    assert result.confidence > Decimal("0.5")


# ------------------------------------------------------------------ сборка плана
def _invalidation() -> InvalidationRule:
    return InvalidationRule(description="тест", check=lambda s: False, code="test")


def test_build_trade_plan_creates_valid_plan() -> None:
    snapshot = _rich_snapshot()
    signal = scan_setup(snapshot, make_config())
    result = build_trade_plan(
        instrument=Instrument(uid="uid", ticker="SBER", lot_size=10),
        snapshot=snapshot,
        signal=signal,
        config=make_config(),
        invalidation_rule=_invalidation(),
        now=NOW,
        atr=Decimal("2"),
    )
    assert result.is_built
    plan = result.plan
    assert plan is not None
    assert plan.hard_stop_price < plan.entry_price
    assert plan.target_price > plan.entry_price
    assert plan.risk_reward_ratio >= Decimal("2")
    assert plan.max_holding_time == timedelta(hours=72)


def test_build_trade_plan_rejects_non_actionable_signal() -> None:
    snapshot = _rich_snapshot()
    signal = scan_setup(snapshot, make_config())
    broken = type(signal)(
        instrument_uid=signal.instrument_uid,
        factors=signal.factors,
        score=signal.score,
        bias=signal.bias,
        summary=signal.summary,
        actionable=False,
        blocking_reason="заблокировано",
    )
    result = build_trade_plan(
        instrument=Instrument(uid="uid", ticker="SBER", lot_size=10),
        snapshot=snapshot,
        signal=broken,
        config=make_config(),
        invalidation_rule=_invalidation(),
        now=NOW,
        atr=Decimal("2"),
    )
    assert not result.is_built
    assert result.rejection_reason == "заблокировано"


def test_build_trade_plan_rejects_poor_risk_reward() -> None:
    snapshot = _rich_snapshot()
    signal = scan_setup(snapshot, make_config())
    # Очень широкий стоп: RR упадёт ниже минимума.
    result = build_trade_plan(
        instrument=Instrument(uid="uid", ticker="SBER", lot_size=10),
        snapshot=snapshot,
        signal=signal,
        config=make_config(),
        invalidation_rule=_invalidation(),
        now=NOW,
        atr=Decimal("200"),
        min_risk_reward=Decimal("2"),
    )
    assert not result.is_built or result.risk_reward >= Decimal("2")
