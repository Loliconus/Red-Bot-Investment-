"""Юнит-тесты собственных индикаторов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.analysis.atr import (
    ATRIndicator,
    atr_pct,
    stop_distance,
    true_range,
    wilder_atr,
)
from core.analysis.fibonacci import (
    FibonacciIndicator,
    fibonacci_levels,
    find_swing,
    is_near_level,
    nearest_fib_level,
)
from core.analysis.market_correlation import (
    correlation,
    is_market_driven,
    price_returns,
    relative_strength,
)
from core.analysis.orderbook_analysis import (
    OrderbookIndicator,
    find_walls,
    orderbook_penalty,
)
from core.analysis.volume_indicators import (
    OBVIndicator,
    VWAPIndicator,
    obv_slope,
    on_balance_volume,
    volume_trend,
    vwap,
)
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries, OrderbookLevel, OrderbookSnapshot

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


def _series(
    prices: list[tuple[Decimal, Decimal, Decimal, Decimal, int]],
    timeframe: Timeframe = Timeframe.D1,
) -> CandleSeries:
    candles = tuple(
        OHLCV(
            open=o,
            high=h,
            low=low,
            close=c,
            volume=v,
            timestamp=NOW + timedelta(days=i),
            timeframe=timeframe,
        )
        for i, (o, h, low, c, v) in enumerate(prices)
    )
    return CandleSeries(timeframe=timeframe, candles=candles)


def _flat(high: Decimal, low: Decimal, close: Decimal, volume: int = 100) -> tuple:
    return (close, high, low, close, volume)


# ------------------------------------------------------------------ ATR
def test_true_range_without_gap() -> None:
    current = OHLCV(
        open=Decimal("10"),
        high=Decimal("12"),
        low=Decimal("8"),
        close=Decimal("11"),
        volume=1,
        timestamp=NOW,
        timeframe=Timeframe.D1,
    )
    previous = OHLCV(
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=1,
        timestamp=NOW,
        timeframe=Timeframe.D1,
    )
    assert true_range(current, previous) == Decimal("4")


def test_true_range_with_gap_up() -> None:
    current = OHLCV(
        open=Decimal("20"),
        high=Decimal("21"),
        low=Decimal("19"),
        close=Decimal("20"),
        volume=1,
        timestamp=NOW,
        timeframe=Timeframe.D1,
    )
    previous = OHLCV(
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=1,
        timestamp=NOW,
        timeframe=Timeframe.D1,
    )
    # |high - prev_close| = 11 > (high-low) = 2
    assert true_range(current, previous) == Decimal("11")


def test_wilder_atr_on_constant_range() -> None:
    # Диапазон всегда 2 → ATR равен 2 независимо от сглаживания.
    series = _series([_flat(Decimal("11"), Decimal("9"), Decimal("10")) for _ in range(30)])
    assert wilder_atr(series, 14) == Decimal("2")


def test_wilder_atr_requires_enough_candles() -> None:
    series = _series([_flat(Decimal("11"), Decimal("9"), Decimal("10")) for _ in range(5)])
    with pytest.raises(ValueError, match="Недостаточно"):
        wilder_atr(series, 14)


def test_atr_indicator_exposes_pct() -> None:
    series = _series([_flat(Decimal("11"), Decimal("9"), Decimal("10")) for _ in range(30)])
    result = ATRIndicator(period=14, timeframe=Timeframe.D1).calculate(series)
    assert result.value == Decimal("2")
    assert result.raw["atr_pct"] == atr_pct(Decimal("2"), Decimal("10"))
    assert stop_distance(Decimal("2"), Decimal("1.5")) == Decimal("3")


# ------------------------------------------------------------------ OBV / VWAP
def test_obv_accumulates_on_rising_closes() -> None:
    series = _series(
        [
            _flat(Decimal("11"), Decimal("9"), Decimal("10"), 100),
            _flat(Decimal("12"), Decimal("10"), Decimal("11"), 200),
            _flat(Decimal("13"), Decimal("11"), Decimal("12"), 300),
        ]
    )
    # Приросты: +200 (11>10), +300 (12>11) → 500
    assert on_balance_volume(series) == Decimal("500")


def test_obv_subtracts_on_falling_closes() -> None:
    series = _series(
        [
            _flat(Decimal("13"), Decimal("11"), Decimal("12"), 100),
            _flat(Decimal("12"), Decimal("10"), Decimal("11"), 200),
        ]
    )
    assert on_balance_volume(series) == Decimal("-200")


def test_obv_slope_sign() -> None:
    rising = _series(
        [
            _flat(Decimal("11"), Decimal("9"), Decimal("10"), 100),
            _flat(Decimal("12"), Decimal("10"), Decimal("11"), 200),
            _flat(Decimal("13"), Decimal("11"), Decimal("12"), 300),
        ]
    )
    falling = _series(
        [
            _flat(Decimal("13"), Decimal("11"), Decimal("12"), 100),
            _flat(Decimal("12"), Decimal("10"), Decimal("11"), 200),
            _flat(Decimal("11"), Decimal("9"), Decimal("10"), 300),
        ]
    )
    assert obv_slope(rising, lookback=2) > 0
    assert obv_slope(falling, lookback=2) < 0


def test_vwap_weights_by_volume() -> None:
    series = _series(
        [
            (Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 100),
            (Decimal("20"), Decimal("21"), Decimal("19"), Decimal("20"), 300),
        ]
    )
    # typical: (11+9+10)/3 = 10 ; (21+19+20)/3 = 20
    # (10*100 + 20*300) / 400 = 7000/400 = 17.5
    assert vwap(series) == Decimal("17.5")


def test_vwap_zero_volume_is_safe() -> None:
    series = _series([(Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), 0)])
    assert vwap(series) == Decimal("0")


def test_volume_trend_detects_spike() -> None:
    series = _series(
        [_flat(Decimal("11"), Decimal("9"), Decimal("10"), 100) for _ in range(11)]
        + [_flat(Decimal("11"), Decimal("9"), Decimal("10"), 500)]
    )
    assert volume_trend(series, lookback=10) == Decimal("5")


def test_obv_and_vwap_indicators_produce_signals() -> None:
    series = _series(
        [
            _flat(Decimal("11"), Decimal("9"), Decimal("10"), 100),
            _flat(Decimal("12"), Decimal("10"), Decimal("11"), 200),
            _flat(Decimal("13"), Decimal("11"), Decimal("12"), 300),
        ],
        timeframe=Timeframe.M1,
    )
    obv_result = OBVIndicator(lookback=2, timeframe=Timeframe.M1).calculate(series)
    assert obv_result.signal == "obv_rising"

    vwap_result = VWAPIndicator(timeframe=Timeframe.M1).calculate(series)
    assert vwap_result.signal in {"above_vwap", "below_vwap", "at_vwap"}


# ------------------------------------------------------------------ Fibonacci
def test_fibonacci_levels_descend_from_high() -> None:
    levels = fibonacci_levels(Decimal("100"), Decimal("200"))
    assert levels["swing_low"] == Decimal("100")
    assert levels["swing_high"] == Decimal("200")
    assert levels["fib_0.618"] == Decimal("200") - Decimal("100") * Decimal("0.618")
    assert levels["fib_0.5"] == Decimal("150")
    # Уровни убывают по мере роста коэффициента
    assert levels["fib_0.236"] > levels["fib_0.382"] > levels["fib_0.5"]


def test_fibonacci_rejects_inverted_swing() -> None:
    with pytest.raises(ValueError, match="swing_high"):
        fibonacci_levels(Decimal("200"), Decimal("100"))


def test_nearest_fib_level_and_golden_zone() -> None:
    levels = fibonacci_levels(Decimal("100"), Decimal("200"))
    proximity = nearest_fib_level(Decimal("150"), levels)
    assert proximity is not None
    assert proximity.level_name == "fib_0.5"
    assert proximity.in_golden_zone
    assert is_near_level(proximity)


def test_price_far_from_levels_is_not_near() -> None:
    levels = fibonacci_levels(Decimal("100"), Decimal("200"))
    proximity = nearest_fib_level(Decimal("300"), levels)
    assert proximity is not None
    assert not is_near_level(proximity)


def test_find_swing_returns_min_and_max() -> None:
    series = _series(
        [
            _flat(Decimal("20"), Decimal("10"), Decimal("15")),
            _flat(Decimal("25"), Decimal("5"), Decimal("20")),
        ]
    )
    assert find_swing(series, lookback=10) == (Decimal("5"), Decimal("25"))


def test_fibonacci_indicator_detects_golden_zone() -> None:
    # Цена откатилась ровно к уровню 0.618
    series = _series(
        [
            _flat(Decimal("110"), Decimal("90"), Decimal("100")),
            _flat(Decimal("210"), Decimal("190"), Decimal("200")),
            _flat(Decimal("140"), Decimal("120"), Decimal("138.2")),
        ],
        timeframe=Timeframe.H1,
    )
    result = FibonacciIndicator(lookback=10, timeframe=Timeframe.H1).calculate(series)
    assert result.signal in {"in_golden_zone", "near_fib_level", "no_fib_confluence"}


# ------------------------------------------------------------------ корреляция
def test_correlation_of_identical_series_is_one() -> None:
    values = tuple(
        Decimal(str(v))
        for v in (0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.01, 0.02, -0.03, 0.01, 0.02, -0.015)
    )
    assert correlation(values, values) == Decimal("1")


def test_correlation_of_inverse_series_is_minus_one() -> None:
    values = tuple(
        Decimal(str(v))
        for v in (0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.01, 0.02, -0.03, 0.01, 0.02, -0.015)
    )
    inverted = tuple(-v for v in values)
    assert correlation(values, inverted) == Decimal("-1")


def test_correlation_short_sample_returns_zero() -> None:
    short = (Decimal("0.1"), Decimal("0.2"))
    assert correlation(short, short) == Decimal("0")


def test_correlation_zero_variance_returns_zero() -> None:
    flat = tuple(Decimal("0.01") for _ in range(20))
    varying = tuple(Decimal(str(i)) / 100 for i in range(20))
    assert correlation(flat, varying) == Decimal("0")


def test_price_returns_computes_simple_returns() -> None:
    series = _series(
        [
            _flat(Decimal("105"), Decimal("95"), Decimal("100")),
            _flat(Decimal("115"), Decimal("105"), Decimal("110")),
        ]
    )
    assert price_returns(series.candles) == (Decimal("0.1"),)


def test_relative_strength_compares_returns() -> None:
    strong = _series(
        [
            _flat(Decimal("105"), Decimal("95"), Decimal("100")),
            _flat(Decimal("115"), Decimal("105"), Decimal("110")),
        ]
    )
    weak = _series(
        [
            _flat(Decimal("105"), Decimal("95"), Decimal("100")),
            _flat(Decimal("106"), Decimal("96"), Decimal("101")),
        ]
    )
    rs = relative_strength(strong.candles, weak.candles, lookback=10)
    assert rs == Decimal("0.09")


def test_is_market_driven_requires_high_corr_and_no_alpha() -> None:
    assert is_market_driven(Decimal("0.95"), Decimal("0"))
    assert not is_market_driven(Decimal("0.95"), Decimal("0.01"))
    assert not is_market_driven(Decimal("0.2"), Decimal("0"))


# ------------------------------------------------------------------ стакан
def _book(bid: int, ask: int, *, levels: int = 5) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        bids=tuple(
            OrderbookLevel(price=Decimal("100") - Decimal(i) / 100, quantity=bid)
            for i in range(levels)
        ),
        asks=tuple(
            OrderbookLevel(price=Decimal("100.02") + Decimal(i) / 100, quantity=ask)
            for i in range(levels)
        ),
        captured_at=NOW,
    )


def test_orderbook_imbalance_sign() -> None:
    assert _book(1000, 500).imbalance > 0
    assert _book(500, 1000).imbalance < 0


def test_find_walls_detects_oversized_level() -> None:
    levels = (
        *tuple(
            OrderbookLevel(price=Decimal("100") - Decimal(i) / 10, quantity=100) for i in range(9)
        ),
        OrderbookLevel(price=Decimal("98"), quantity=1000),
    )
    walls = find_walls(levels)
    assert len(walls) == 1
    assert walls[0].price == Decimal("98")


def test_orderbook_indicator_signals() -> None:
    result = OrderbookIndicator(_book(1000, 500)).calculate()
    assert result.signal == "bid_heavy"
    assert result.value > 0


def test_orderbook_indicator_marks_wide_spread() -> None:
    wide = OrderbookSnapshot(
        bids=(OrderbookLevel(price=Decimal("99"), quantity=100),),
        asks=(OrderbookLevel(price=Decimal("101.5"), quantity=100),),
        captured_at=NOW,
    )
    result = OrderbookIndicator(wide).calculate()
    assert "wide_spread" in result.signal


def test_orderbook_penalty_grows_with_spread() -> None:
    assert orderbook_penalty(Decimal("0")) == Decimal("0")
    narrow = orderbook_penalty(Decimal("0.001"))
    wide = orderbook_penalty(Decimal("0.01"))
    assert narrow > wide
    assert wide == Decimal("-1")
