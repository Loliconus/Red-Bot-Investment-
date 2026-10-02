"""Граница ML float → проверенные Decimal DTO → один общий симулятор."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from core.backtest.probability import ProbabilityBacktester, ProbabilityBacktestResult
from core.domain.probability import MarketProbabilities, ProbabilityRegime, ResearchBar
from synthetic_trader.config import ExperimentConfig, RiskConfig
from synthetic_trader.data import MarketDataset
from synthetic_trader.features import rolling_correlations
from synthetic_trader.statistics import daily_returns, performance_metrics, return_metrics


def decimal_atr_history(
    dataset: MarketDataset, config: ExperimentConfig
) -> dict[tuple[str, datetime], Decimal]:
    """Финансовый ATR отдельно от float feature ATR: Wilder RMA по Decimal OHLC."""
    values: dict[tuple[str, datetime], Decimal] = {}
    period = Decimal(config.atr_period)
    for symbol in config.symbols:
        previous: Decimal | None = None
        average: Decimal | None = None
        frame = dataset.frames[(symbol, config.interval)]
        for count, row in enumerate(frame.itertuples(index=False), 1):
            high, low, close = Decimal(row.high), Decimal(row.low), Decimal(row.close)
            tr = (
                high - low
                if previous is None
                else max(high - low, abs(high - previous), abs(low - previous))
            )
            average = tr if average is None else (average * (period - 1) + tr) / period
            if count >= config.atr_period:
                values[(symbol, row.end.to_pydatetime())] = average
            previous = close
    return values


def signals_from_predictions(
    predicted: pd.DataFrame, atr_values: dict[tuple[str, datetime], Decimal]
) -> list[MarketProbabilities]:
    signals: list[MarketProbabilities] = []
    for row in predicted.to_dict("records"):
        signals.append(
            MarketProbabilities(
                symbol=row["symbol"],
                asof=row["asof"].to_pydatetime(),
                trend=Decimal(str(row["p_trend"])),
                up_given_trend=Decimal(str(row["p_up"])),
                break_within_h=Decimal(str(row["p_break"])),
                atr=atr_values[(row["symbol"], row["asof"].to_pydatetime())],
                volatility=Decimal(str(max(row["rv_20"], 1e-9))),
                regime=ProbabilityRegime(row["regime"]),
                panic_probability=Decimal(str(row["regime_panic"])),
            )
        )
    return signals


def simulation_bars(
    dataset: MarketDataset, config: ExperimentConfig, begin: pd.Timestamp, end: pd.Timestamp
) -> list[ResearchBar]:
    bars: list[ResearchBar] = []
    for symbol in config.symbols:
        frame = dataset.frames[(symbol, config.interval)]
        subset = frame.loc[(frame["begin"] >= begin) & (frame["end"] <= end)]
        for row in subset.to_dict("records"):
            bars.append(
                ResearchBar(
                    symbol=symbol,
                    begin=row["begin"].to_pydatetime(),
                    end=row["end"].to_pydatetime(),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                )
            )
    return bars


def run_backtest(
    dataset: MarketDataset,
    config: ExperimentConfig,
    predicted: pd.DataFrame,
    *,
    threshold: Decimal | None = None,
    correlations: dict[datetime, dict[tuple[str, str], Decimal]] | None = None,
) -> ProbabilityBacktestResult:
    policy = config.risk.policy(config.horizon)
    if threshold is not None:
        policy = replace(policy, trend_threshold=threshold)
    end = predicted["label_end"].max() if "label_end" in predicted else predicted["asof"].max()
    bars = simulation_bars(dataset, config, predicted["asof"].min(), end)
    simulator = ProbabilityBacktester(
        instruments=dataset.instruments, policy=policy, initial_capital=config.initial_capital
    )
    return simulator.run(
        bars,
        signals_from_predictions(predicted, decimal_atr_history(dataset, config)),
        correlations=correlations
        if correlations is not None
        else rolling_correlations(dataset, config),
    )


def candidate_thresholds(config: ExperimentConfig) -> list[Decimal]:
    # План кандидатов фиксируется/логируется ДО outer test, не ищется на final.
    return list(
        dict.fromkeys(
            (
                config.risk.trend_threshold,
                Decimal("0.50"),
                Decimal("0.60"),
                Decimal("0.70"),
                Decimal("0.80"),
            )
        )
    )


def imoex_reference(
    dataset: MarketDataset, config: ExperimentConfig, timestamps: pd.DatetimeIndex
) -> pd.Series:
    frame = dataset.frames[("IMOEX", config.interval)]
    subset = frame.loc[(frame["begin"] >= timestamps.min()) & (frame["end"] <= timestamps.max())]
    if subset.empty:
        raise ValueError("Нет IMOEX на том же OOS периоде")
    commission, slippage = config.risk.commission_bps / 10000, config.risk.slippage_bps / 10000
    entry = Decimal(subset.iloc[0]["open"]) * (1 + slippage)
    units = config.initial_capital / (entry * (1 + commission))
    # Теоретический дробный price-index benchmark, НЕ торгуемый IMOEX / ETF.
    points = pd.Series(
        [units * Decimal(p) for p in subset["close"]], index=pd.DatetimeIndex(subset["end"])
    )
    aligned = points.reindex(timestamps, method="ffill").fillna(config.initial_capital)
    aligned.iloc[0] = config.initial_capital
    aligned.iloc[-1] *= (1 - commission) * (1 - slippage)
    # Только после расчёта всех финансовых издержек — граница статистических float.
    daily = (
        aligned.groupby(aligned.index.tz_convert("Europe/Moscow").normalize()).last().astype(float)
    )
    returns = daily.pct_change()
    returns.iloc[0] = daily.iloc[0] / float(config.initial_capital) - 1
    return returns.astype(float)


def benchmark_predictions(rows: pd.DataFrame, method: str) -> pd.DataFrame:
    result = rows[["symbol", "asof", "atr", "rv_20", "label_end"]].copy()
    result["p_trend"], result["p_break"], result["regime_panic"] = 0.99, 0.0, 0.0
    result["regime"] = "range"
    if method == "ma":
        result["p_up"] = (rows["ema_dist_10"] < rows["ema_dist_40"]).map({True: 0.85, False: 0.15})
    elif method == "rsi":
        # RSI обязательный только для baseline, даже при отключённом широком layer B.
        from synthetic_trader.features import rsi

        result["p_up"] = 0.15
        for symbol in rows["symbol"].unique():
            asset = rows.loc[rows["symbol"] == symbol].sort_values("asof")
            values = (
                asset["rsi_14"] * 100 if "rsi_14" in asset else rsi(asset["close_reference"], 14)
            )
            holding = False
            for index, value in values.items():
                if value < 30:
                    holding = True
                elif value > 70:
                    holding = False
                result.at[index, "p_up"] = 0.85 if holding else 0.15
    else:
        raise ValueError("Неизвестный benchmark")
    return result


def run_benchmarks(
    dataset: MarketDataset,
    config: ExperimentConfig,
    oos_rows: pd.DataFrame,
    primary: ProbabilityBacktestResult,
    correlations: dict[datetime, dict[tuple[str, str], Decimal]],
) -> tuple[dict[str, Any], pd.Series]:
    timestamps = pd.DatetimeIndex([p.timestamp for p in primary.equity])
    imoex = imoex_reference(dataset, config, timestamps)
    reports: dict[str, Any] = {
        "imoex": {
            "name": "IMOEX · price-index reference",
            "metrics": return_metrics(imoex),
            "daily_returns": [
                {"date": date.isoformat(), "return": float(value)} for date, value in imoex.items()
            ],
            "tradable": False,
            "dividends_included": False,
        }
    }
    # Тот же ledger, капитал, risk limits и costs. Порог ниже фиксированной 0.99.
    benchmark_risk = RiskConfig(**{**config.risk.model_dump(), "trend_threshold": Decimal("0.50")})
    benchmark_config = config.model_copy(update={"risk": benchmark_risk})
    for method, name in (("ma", "EMA 10 / 40"), ("rsi", "RSI 14 · 30 / 70")):
        predicted = benchmark_predictions(oos_rows, method)
        result = run_backtest(dataset, benchmark_config, predicted, correlations=correlations)
        reports[method] = {
            "name": name,
            "metrics": performance_metrics(result),
            "daily_returns": [
                {"date": date.isoformat(), "return": float(value)}
                for date, value in daily_returns(result).items()
            ],
            "tradable": True,
            "risk_overlay": "same long-only risk/cost engine",
        }
    return reports, imoex
