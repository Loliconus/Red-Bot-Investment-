"""Daily net-return diagnostics: DSR (raw kurtosis), CSCV PBO и stationary SPA/RC."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd
from arch.bootstrap import SPA
from numpy.typing import NDArray
from purgedcv import deflated_sharpe_ratio_full, probability_of_backtest_overfitting

from core.backtest.probability import ProbabilityBacktestResult
from synthetic_trader.storage import json_safe


def daily_equity(result: ProbabilityBacktestResult) -> pd.Series:
    equity = pd.Series(
        [float(p.equity) for p in result.equity],
        index=pd.DatetimeIndex([p.timestamp for p in result.equity]).tz_convert("Europe/Moscow"),
    )
    # Только наблюдавшиеся торговые дни: не добавляем выходным нулевые returns.
    return equity.groupby(equity.index.normalize()).last()


def daily_returns(result: ProbabilityBacktestResult) -> pd.Series:
    equity = daily_equity(result)
    returns = equity.pct_change()
    # Первый торговый день также несёт все entry fees / slippage.
    returns.iloc[0] = equity.iloc[0] / float(result.equity[0].equity) - 1
    return returns.astype(float)


def return_metrics(returns: pd.Series) -> dict[str, Any]:
    values = returns.to_numpy(dtype=float)
    if not len(values) or not np.isfinite(values).all() or (values <= -1).any():
        return {
            "net_return": None,
            "sharpe": None,
            "max_drawdown": None,
            "cagr": None,
            "days": len(values),
        }
    wealth = np.r_[1.0, np.cumprod(1 + values)]
    peak = np.maximum.accumulate(wealth)
    std = values.std(ddof=1) if len(values) > 1 else 0.0
    elapsed = max(1.0, (returns.index[-1] - returns.index[0]).total_seconds() / 86400)
    return {
        "net_return": float(wealth[-1] - 1),
        "max_drawdown": float(np.min(wealth / peak - 1)),
        "sharpe": float(values.mean() / std * np.sqrt(252)) if std > 1e-12 else None,
        "cagr": float(wealth[-1] ** (365.25 / elapsed) - 1) if elapsed >= 180 else None,
        "days": len(values),
        "annualization": 252,
        "risk_free_rate": 0.0,
    }


def performance_metrics(result: ProbabilityBacktestResult) -> dict[str, Any]:
    pnl = [float(t.net_pnl) for t in result.trades]
    gross_positive = sum(p for p in pnl if p > 0)
    gross_negative = -sum(p for p in pnl if p < 0)
    return {
        **return_metrics(daily_returns(result)),
        "trades": len(pnl),
        "win_rate": sum(p > 0 for p in pnl) / len(pnl) if pnl else None,
        "profit_factor": gross_positive / gross_negative if gross_negative else None,
        "fees": str(sum((t.fees for t in result.trades), start=result.equity[0].cash * 0)),
        "kill_reason": result.kill_reason,
        "ambiguous_execution_bars": result.ambiguous_execution_bars,
        "rejections": result.rejections,
    }


def multiple_testing(
    returns: pd.DataFrame,
    benchmark: pd.Series,
    *,
    primary: str,
    trials: int,
    repetitions: int,
    seed: int,
    trial_variance: float | None = None,
) -> dict[str, Any]:
    if (
        not returns.index.equals(benchmark.index)
        or not np.isfinite(returns.to_numpy()).all()
        or not np.isfinite(benchmark.to_numpy()).all()
    ):
        raise ValueError("Статистические тесты требуют общей конечной daily return matrix")
    result: dict[str, Any] = {
        "trials": trials,
        "candidate_count": len(returns.columns),
        "frequency": "daily_net_returns",
        "days": len(returns),
        "trial_accounting": "append-only study ledger (включая failed attempts) + additional_trials; ручные попытки неизвестны",
        "limitations": "CPCV/CSCV пути зависимы. DSR Gaussian approximation не заменяет final OOS и stationarity diagnostics.",
    }
    if len(returns) < 40 or returns[primary].std() < 1e-12:
        reason = "Недостаточно дней / ненулевая variance / сделок для статистического вывода"
        return {
            **result,
            "dsr": None,
            "pbo": None,
            "spa": None,
            "reality_check": None,
            "unavailable_reason": reason,
        }
    sharpes = np.asarray(
        [col.mean() / col.std(ddof=0) for _, col in returns.items() if col.std(ddof=0) > 1e-12]
    )
    empirical_variance = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else 0.0
    # Защита от нулевой cross-trial variance у очень похожих кандидатов.
    if trial_variance is not None and (not np.isfinite(trial_variance) or trial_variance < 0):
        raise ValueError("Нужна конечная неотрицательная variance Sharpe")
    variance = max(empirical_variance, trial_variance or 0.0, 1 / (len(returns) - 1))
    try:
        diagnostics = deflated_sharpe_ratio_full(
            returns[primary].to_numpy(), n_trials=max(1, trials), var_sharpe=variance
        )
        result["dsr"] = json_safe(asdict(diagnostics))
        result["dsr"]["variance_source"] = (
            "max(current/ledger daily Sharpe variance, 1/(T-1)) — conservative sampling floor"
        )
    except ValueError as exc:
        result["dsr"], result["dsr_unavailable"] = None, str(exc)
    distinct = np.unique(returns.to_numpy().T, axis=0)
    if len(distinct) < 2:
        result["pbo"], result["pbo_unavailable"] = None, "Все кандидаты дали одну equity curve"
    else:
        pbo = probability_of_backtest_overfitting(returns.to_numpy().T, n_splits=8)
        result["pbo"] = {
            "probability": float(pbo.pbo),
            "combinations": int(pbo.n_combos),
            "logits": pbo.logits.tolist(),
            "slope": json_safe(pbo.slope),
        }
    block = max(2, int(np.sqrt(len(returns))))
    result["bootstrap"] = {
        "method": "stationary",
        "block_size": block,
        "reps": repetitions,
        "seed": seed,
    }
    # arch принимает LOSSES, не returns. Оба теста сравнивают с тем же IMOEX.
    losses, models = -benchmark.to_numpy(), -returns.to_numpy()
    # Дегенеративные loss differences не studentize-ятся, не делим на zero sigma.
    useful = np.std(losses[:, None] - models, axis=0) > 1e-12
    if not useful.any():
        result["spa"], result["reality_check"] = None, None
        result["spa_unavailable"] = "Zero-variance differential losses"
        return result
    spa = SPA(
        losses,
        models[:, useful],
        block_size=block,
        reps=repetitions,
        bootstrap="stationary",
        studentize=True,
        seed=seed,
    )
    spa.compute()
    result["spa"] = {
        "p_value": float(spa.pvalues["consistent"]),
        "lower": float(spa.pvalues["lower"]),
        "upper": float(spa.pvalues["upper"]),
    }
    # White RC: non-studentized statistic, upper = full-null recentering;
    # не выдаём alias arch.RealityCheck с defaults SPA за отдельный White test.
    white = SPA(
        losses,
        models[:, useful],
        block_size=block,
        reps=repetitions,
        bootstrap="stationary",
        studentize=False,
        seed=seed,
    )
    white.compute()
    result["reality_check"] = {
        "p_value": float(white.pvalues["upper"]),
        "method": "arch SPA, studentize=False, upper full-null recentering",
    }
    return result


def population_stability_index(
    reference_edges: list[float], reference: NDArray[np.float64], observed: NDArray[np.float64]
) -> float | None:
    edges = np.unique(reference_edges)
    if len(edges) < 3:
        return None
    edges = np.r_[-np.inf, edges[1:-1], np.inf]  # tail observations не теряются
    expected, _ = np.histogram(reference[np.isfinite(reference)], bins=edges)
    actual, _ = np.histogram(observed[np.isfinite(observed)], bins=edges)
    if expected.sum() == 0 or actual.sum() == 0:
        return None
    e = (expected + 0.5) / (expected.sum() + 0.5 * len(expected))
    a = (actual + 0.5) / (actual.sum() + 0.5 * len(actual))
    return float(np.sum((a - e) * np.log(a / e)))
