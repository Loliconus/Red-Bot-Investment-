"""Относительная сила инструмента против бенчмарка IMOEX.

IMOEX в системе — неторгуемый эталон: сделка открывается, только если бумага
сильнее рынка. Попытка обойти это условие бессмысленна: условие зашито в
``RiskManager`` перед выставлением ордера.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise

from core.analysis.protocols import IndicatorResult
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV, CandleSeries

ZERO = Decimal("0")
ONE = Decimal("1")

#: Минимальная длина выборки для осмысленной корреляции.
MIN_SAMPLE = 10
#: Порог, при котором инструмент считается «ведомым рынком».
HIGH_CORRELATION = Decimal("0.7")


def price_returns(candles: Sequence[OHLCV]) -> tuple[Decimal, ...]:
    """Простые доходности: ``(C_t − C_{t−1}) / C_{t−1}``."""
    closes = [c.close for c in candles]
    result: list[Decimal] = []
    for previous, current in pairwise(closes):
        if previous == ZERO:
            continue
        result.append((current - previous) / previous)
    return tuple(result)


def correlation(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal:
    """Коэффициент корреляции Пирсона.

    Возвращает 0 при вырожденной выборке (слишком короткой или с нулевой
    дисперсией) — это безопаснее, чем кидать исключение в торговом цикле.
    """
    n = min(len(xs), len(ys))
    if n < MIN_SAMPLE:
        return ZERO

    a = xs[-n:]
    b = ys[-n:]
    mean_a = sum(a, ZERO) / Decimal(n)
    mean_b = sum(b, ZERO) / Decimal(n)

    covariance = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    variance_a: Decimal = sum(((value - mean_a) ** 2 for value in a), ZERO)
    variance_b: Decimal = sum(((value - mean_b) ** 2 for value in b), ZERO)

    if variance_a == ZERO or variance_b == ZERO:
        return ZERO

    denominator = (variance_a * variance_b).sqrt()
    if denominator == ZERO:
        return ZERO
    return max(min(covariance / denominator, ONE), -ONE)


def relative_strength(
    instrument_candles: Sequence[OHLCV],
    benchmark_candles: Sequence[OHLCV],
    *,
    lookback: int = 20,
) -> Decimal:
    """Относительная сила: доходность инструмента минус доходность бенчмарка.

    > 0 — бумага опережает IMOEX, < 0 — отстаёт.
    """
    if not instrument_candles or not benchmark_candles:
        return ZERO

    def period_return(candles: Sequence[OHLCV]) -> Decimal:
        window = candles[-lookback:] if len(candles) > lookback else candles
        first, last = window[0].close, window[-1].close
        if first == ZERO:
            return ZERO
        return (last - first) / first

    return period_return(instrument_candles) - period_return(benchmark_candles)


class MarketCorrelationIndicator:
    """Корреляция и относительная сила против IMOEX."""

    name = "market_correlation"

    __slots__ = ("benchmark", "lookback", "timeframe")

    def __init__(
        self,
        benchmark: CandleSeries,
        *,
        lookback: int = 20,
        timeframe: Timeframe = Timeframe.D1,
    ) -> None:
        self.benchmark = benchmark
        self.lookback = lookback
        self.timeframe = timeframe

    def calculate(self, series: CandleSeries) -> IndicatorResult:
        inst_returns = price_returns(series.candles)
        bench_returns = price_returns(self.benchmark.candles)
        corr = correlation(inst_returns, bench_returns)
        rs = relative_strength(series.candles, self.benchmark.candles, lookback=self.lookback)

        if rs > ZERO:
            signal = "outperforming_market"
        elif rs < ZERO:
            signal = "lagging_market"
        else:
            signal = "in_line_with_market"

        return IndicatorResult(
            value=rs,
            signal=signal,
            raw={"correlation": corr, "relative_strength": rs},
        )


def is_market_driven(corr: Decimal, rs: Decimal) -> bool:
    """Истина, если движение бумаги — это просто бета рынка, а не её идея."""
    return corr >= HIGH_CORRELATION and rs <= ZERO
