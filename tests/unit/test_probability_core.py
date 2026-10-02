"""Pure domain safety: стандартная библиотека, Decimal, ни сети, ни SDK, ни диска."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.backtest.probability import ProbabilityBacktester
from core.domain.probability import (
    MarketProbabilities,
    ProbabilityRegime,
    ProbabilityRiskPolicy,
    ResearchBar,
    ResearchInstrument,
)
from core.risk.probability import Exposure, size_probability_position

D = Decimal
NOW = datetime(2025, 1, 10, 9, tzinfo=UTC)
INST = ResearchInstrument(symbol="SBER", lot_size=10, tick_size=D("0.01"), sector="finance")
POLICY = ProbabilityRiskPolicy(commission_bps=D("0"), slippage_bps=D("0"))


def signal(symbol="SBER", asof=NOW + timedelta(hours=1), **values):
    return MarketProbabilities(
        symbol=symbol,
        asof=asof,
        trend=values.get("trend", D("0.99")),
        up_given_trend=values.get("up", D("0.99")),
        break_within_h=values.get("broken", D("0.01")),
        atr=D("1"),
        volatility=values.get("vol", D("0.003")),
        regime=values.get("regime", ProbabilityRegime.TREND),
    )


def bar(index, *, symbol="SBER", open_="100", high="101", low="99", close="100", begin=None):
    start = begin or NOW + timedelta(hours=index)
    return ResearchBar(
        symbol=symbol,
        begin=start,
        end=start + timedelta(hours=1),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D("1000"),
    )


def size(sig=None, **kwargs):
    return size_probability_position(
        signal=sig or signal(),
        instrument=kwargs.get("instrument", INST),
        entry_price=D("100"),
        equity=D("100000"),
        cash=kwargs.get("cash", D("100000")),
        exposures=kwargs.get("exposures", []),
        correlations=kwargs.get("correlations", {}),
        policy=kwargs.get("policy", POLICY),
        killed=kwargs.get("killed", False),
    )


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-0.1"), D("1.01"), 0.8])
def test_invalid_probabilities_fail_closed(value):
    with pytest.raises(ValueError):
        signal(trend=value)


def test_naive_time_is_rejected():
    with pytest.raises(ValueError, match="UTC"):
        signal(asof=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("sig", "reason"),
    [
        (signal(trend=D("0.60")), "weak_trend"),
        (signal(up=D("0.4")), "no_long_edge"),
        (signal(broken=D("0.9")), "break_risk"),
        (signal(regime=ProbabilityRegime.PANIC), "panic_regime"),
    ],
)
def test_probability_gates(sig, reason):
    assert size(sig).reason == reason


def test_break_risk_and_volatility_reduce_lots():
    assert 0 < size(signal(broken=D("0.5"))).lots < size().lots
    assert 0 < size(signal(vol=D("0.03"))).lots < size().lots


def test_benchmark_and_kill_are_not_tradable():
    anchor = replace(INST, symbol="IMOEX", is_benchmark=True)
    assert size(signal(symbol="IMOEX"), instrument=anchor).reason == "benchmark_not_tradable"
    assert size(killed=True).reason == "kill_switch"


def test_correlation_and_sector_caps():
    exposures = [Exposure(symbol="VTBR", sector="finance", notional=D("35000"))]
    assert size(exposures=exposures).reason == "correlation_unknown"
    assert (
        size(exposures=exposures, correlations={("SBER", "VTBR"): D("0.95")}).reason
        == "correlated_position"
    )
    assert (
        size(exposures=exposures, correlations={("SBER", "VTBR"): D("0.2")}).reason
        == "exposure_limit"
    )


def test_lots_ticks_costs_and_cash():
    result = size(cash=D("3500"))
    assert result.lots == 3
    assert result.stop_price % INST.tick_size == 0
    assert result.target_price % INST.tick_size == 0
    expensive = replace(POLICY, commission_bps=D("500"))
    assert size(policy=expensive).reason == "costs_exceed_reward"


def simulate(bars, signals, policy=POLICY, instruments=(INST,), correlations=None):
    return ProbabilityBacktester(
        instruments=instruments, policy=policy, initial_capital=D("100000")
    ).run(bars, signals, correlations=correlations)


def test_close_signal_cannot_fill_its_own_bar():
    result = simulate([bar(0, high="120", low="80")], [signal()])
    assert not result.trades
    result = simulate([bar(0), bar(1, open_="101", high="102", low="100", close="101")], [signal()])
    assert result.trades[0].opened_at == NOW + timedelta(hours=1)
    assert result.trades[0].entry_price == D("101")


def test_stop_wins_ambiguous_intrabar_order():
    result = simulate([bar(0), bar(1, high="104", low="97")], [signal()])
    assert result.trades[0].reason == "stop"
    assert result.trades[0].exit_price == D("98")
    assert result.ambiguous_execution_bars == 1


def test_trailing_stop_never_uses_this_bars_future_high():
    result = simulate(
        [
            bar(0),
            bar(1, high="102", low="99", close="101"),
            bar(2, open_="101", high="101.2", low="100.7", close="101.1"),
        ],
        [signal()],
    )
    assert result.trades[0].reason == "end_of_sample"
    assert result.trades[0].net_pnl > 0


def test_gap_stop_is_worse_than_trigger_and_daily_limit_is_latched():
    tomorrow = NOW + timedelta(days=1)
    result = simulate(
        [
            bar(0),
            bar(1),
            bar(2, open_="50", high="51", low="49", close="50", begin=tomorrow),
            bar(3, begin=tomorrow + timedelta(hours=1)),
        ],
        [signal(), signal(asof=tomorrow + timedelta(hours=1))],
    )
    assert result.trades[0].reason == "gap_stop"
    assert result.trades[0].exit_price == D("50")
    assert result.kill_reason == "daily_drawdown_limit"
    assert len(result.trades) == 1


def test_all_assets_open_before_any_future_close():
    other = ResearchInstrument(symbol="GMKN", lot_size=10, tick_size=D("0.01"), sector="metals")
    policy = replace(POLICY, take_atr=D("200"), risk_per_trade=D("0.05"))
    bars = [
        bar(0),
        bar(0, symbol="GMKN"),
        bar(1, high="200", low="99", close="199"),
        bar(1, symbol="GMKN"),
    ]
    sigs = [signal(), signal(symbol="GMKN")]
    result = simulate(
        bars, sigs, policy, (INST, other), {NOW + timedelta(hours=1): {("GMKN", "SBER"): D("0")}}
    )
    amounts = {t.symbol: t.units for t in result.trades}
    assert amounts["SBER"] == amounts["GMKN"]  # SBER's 199 close cannot size GMKN at earlier open


def test_fees_are_both_sides_and_ledger_is_repeatable():
    policy = replace(POLICY, commission_bps=D("10"), slippage_bps=D("5"))
    simulator = ProbabilityBacktester(
        instruments=(INST,), policy=policy, initial_capital=D("100000")
    )
    first = simulator.run([bar(0), bar(1)], [signal()])
    second = simulator.run([bar(0), bar(1)], [signal()])
    assert first == second
    assert first.trades[0].fees > 0
    assert first.trades[0].net_pnl < 0
    assert first.equity[-1].gross == 0
    assert first.equity[-1].equity == D("100000") + first.trades[0].net_pnl


def test_overlapping_bars_and_duplicate_signals_are_rejected():
    with pytest.raises(ValueError, match="бары"):
        simulate([bar(0), bar(0)], [signal()])
    with pytest.raises(ValueError, match="сигнал"):
        simulate([bar(0), bar(1)], [signal(), signal()])
