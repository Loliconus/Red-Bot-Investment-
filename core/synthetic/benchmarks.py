"""Параллельные эталонные бенчмарки для честного сравнения (раздел 7.2 ТЗ).

Запускаются в идентичных условиях с учётом комиссии брокера и проскальзывания:
1. ``BuyAndHoldBenchmark`` (индекс ``IMOEX`` / базовый актив);
2. ``MovingAverageCrossBot`` (простой пересекающийся тренд-фолловер ``EMA(fast) / EMA(slow)``);
3. ``SimpleMeanReversionRSIBot`` (простой осцилляторный бот ``RSI(14)``).

Критерий допуска к реальным деньгам: «Синтетический трейдер» обязан обыгрывать все три
эталона вне обучающей выборки (Out-of-Sample) **с учётом комиссии и проскальзывания**.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.domain.value_objects import OHLCV
from core.synthetic.validation import compute_annualized_sharpe


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkRunResult:
    """Результаты прогона эталонной стратегии в тех же издержках."""

    name: str
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: float
    trades_count: int
    equity_curve: tuple[Decimal, ...]
    bar_returns: tuple[float, ...]


def _compute_max_drawdown_pct(equity_curve: Sequence[Decimal]) -> Decimal:
    if not equity_curve:
        return Decimal("0")
    peak = equity_curve[0]
    max_dd = Decimal("0")
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > Decimal("0"):
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return (max_dd * Decimal("100")).quantize(Decimal("0.01"))


def run_buy_and_hold_benchmark(
    candles: Sequence[OHLCV],
    *,
    name: str = "Buy & Hold IMOEX",
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 1: Купи-и-держи (Buy-and-Hold IMOEX / базовый актив) с входной/выходной комиссией."""
    n = len(candles)
    if n < 2:
        return BenchmarkRunResult(
            name=name,
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
        )

    cost_rate = commission_rate + slippage_rate
    entry_capital = initial_capital * (Decimal("1") - cost_rate)
    p0 = candles[0].close

    equity: list[Decimal] = [entry_capital]
    bar_rets: list[float] = []

    for i in range(1, n):
        prev_c = candles[i - 1].close
        cur_c = candles[i].close
        r_i = float((cur_c - prev_c) / prev_c) if prev_c > Decimal("0") else 0.0
        bar_rets.append(r_i)
        eq_i = entry_capital * (cur_c / p0) if p0 > Decimal("0") else entry_capital
        if i == n - 1:
            eq_i *= Decimal("1") - cost_rate
        equity.append(eq_i.quantize(Decimal("0.01")))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=name,
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=1,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
    )


def run_ma_crossover_benchmark(
    candles: Sequence[OHLCV],
    *,
    fast_period: int = 8,
    slow_period: int = 21,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 2: Простой бот на пересечении двух скользящих средних (EMA fast / EMA slow)."""
    n = len(candles)
    if n < 3:
        return BenchmarkRunResult(
            name="MA Crossover (8/21)",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
        )

    closes = [float(c.close) for c in candles]
    alpha_f = 2.0 / (fast_period + 1.0)
    alpha_s = 2.0 / (slow_period + 1.0)
    ema_f = closes[0]
    ema_s = closes[0]

    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    in_pos = False
    trades = 0

    for i in range(1, n):
        # Решение принимается по закрытию бара i-1, доходность реализуется на баре i
        prev_c = closes[i - 1]
        cur_c = closes[i]
        raw_ret = (cur_c - prev_c) / prev_c if prev_c > 0 else 0.0

        want_long = ema_f > ema_s and i >= slow_period
        turnover_cost = 0.0
        if want_long != in_pos:
            turnover_cost = cost_rate
            in_pos = want_long
            if in_pos:
                trades += 1

        step_ret = (raw_ret if in_pos else 0.0) - turnover_cost
        eq *= 1.0 + step_ret
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

        # Обновляем EMA по закрытию бара i
        ema_f = alpha_f * cur_c + (1.0 - alpha_f) * ema_f
        ema_s = alpha_s * cur_c + (1.0 - alpha_s) * ema_s

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=f"MA Crossover ({fast_period}/{slow_period})",
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
    )


def run_rsi_benchmark(
    candles: Sequence[OHLCV],
    *,
    period: int = 14,
    oversold: float = 35.0,
    overbought: float = 65.0,
    initial_capital: Decimal = Decimal("1000000"),
    commission_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
) -> BenchmarkRunResult:
    """Эталон 3: Простой контртрендовый бот по RSI(14)."""
    n = len(candles)
    if n < 3:
        return BenchmarkRunResult(
            name="RSI(14) Bot",
            total_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=0.0,
            trades_count=0,
            equity_curve=(initial_capital,),
            bar_returns=(),
        )

    closes = [float(c.close) for c in candles]
    cost_rate = float(commission_rate + slippage_rate)
    eq = float(initial_capital)
    equity: list[Decimal] = [initial_capital]
    bar_rets: list[float] = []
    in_pos = False
    trades = 0

    avg_gain = 0.0
    avg_loss = 0.0

    for i in range(1, n):
        prev_c = closes[i - 1]
        cur_c = closes[i]
        raw_ret = (cur_c - prev_c) / prev_c if prev_c > 0 else 0.0

        # Реализуем доходность текущей позиции, открытой на предыдущем баре
        step_ret = raw_ret if in_pos else 0.0

        # Обновляем RSI на закрытии бара i для решения на следующий бар
        diff = cur_c - prev_c
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        if i <= period:
            avg_gain = ((avg_gain * (i - 1)) + gain) / i
            avg_loss = ((avg_loss * (i - 1)) + loss) / i
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 1e-12 else 50.0

        next_in_pos = in_pos
        if i >= period:
            if not in_pos and rsi < oversold:
                next_in_pos = True
            elif in_pos and rsi > overbought:
                next_in_pos = False

        if next_in_pos != in_pos:
            step_ret -= cost_rate
            if next_in_pos:
                trades += 1
            in_pos = next_in_pos

        eq *= max(0.01, 1.0 + step_ret)
        bar_rets.append(step_ret)
        equity.append(Decimal(f"{eq:.2f}"))

    tot_ret = ((equity[-1] - initial_capital) / initial_capital * Decimal("100")).quantize(
        Decimal("0.01")
    )
    return BenchmarkRunResult(
        name=f"RSI({period}) Bot",
        total_return_pct=tot_ret,
        max_drawdown_pct=_compute_max_drawdown_pct(equity),
        sharpe_ratio=compute_annualized_sharpe(bar_rets),
        trades_count=trades,
        equity_curve=tuple(equity),
        bar_returns=tuple(bar_rets),
    )
