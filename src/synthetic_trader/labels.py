"""Next-open triple barriers и независимый полный horizon break label.

Разметка использует будущее только как TARGET. Фичи l1 full-series запрещены.
Оба барьера в одном OHLC-баре → ambiguous, не произвольная метка UP/DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import osqp
import pandas as pd
from numpy.typing import NDArray
from scipy import sparse

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.features import bar_features


@dataclass(slots=True)
class LabelSet:
    rows: pd.DataFrame
    ambiguous: int
    censored: int


def triple_barrier_labels(frame: pd.DataFrame, config: ExperimentConfig) -> LabelSet:
    data = frame.sort_values("end").reset_index(drop=True)
    features = bar_features(data, atr_period=config.atr_period, layer_b=False)
    atr = features["atr"].to_numpy(dtype=float)
    prices = data[["open", "high", "low", "close"]].astype(float).to_numpy()
    close = data["close"].astype(float)
    bias = np.sign(
        close.ewm(span=10, adjust=False).mean() - close.ewm(span=40, adjust=False).mean()
    ).to_numpy()
    rows: list[dict[str, object]] = []
    ambiguous = 0
    censored = 0
    for i in range(len(data)):
        if i + config.horizon >= len(data):
            censored += 1
            continue
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        entry = prices[i + 1, 0]  # реалистичный вход, НЕ feature
        upper, lower = (
            entry + float(config.upper_atr) * atr[i],
            entry - float(config.lower_atr) * atr[i],
        )
        if lower <= 0:
            continue
        outcome = 0
        resolution = i + config.horizon
        is_ambiguous = False
        for j in range(i + 1, i + config.horizon + 1):
            open_, high, low, _ = prices[j]
            # Gap имеет известный порядок: open виден раньше intrabar extrema.
            if open_ >= upper:
                outcome, resolution = 1, j
                break
            if open_ <= lower:
                outcome, resolution = -1, j
                break
            up, down = high >= upper, low <= lower
            if up and down:
                is_ambiguous, resolution = True, j
                ambiguous += 1
                break
            if up or down:
                outcome, resolution = (1 if up else -1), j
                break
        forward = prices[i + 1 : i + config.horizon + 1]
        # Риск стопа против причинного EMA-bias. Горизонт всегда полный H,
        # даже если triple barrier сработал на первом баре.
        reversal = None
        if bias[i] > 0:
            reversal = int((forward[:, 2] <= entry - float(config.break_atr) * atr[i]).any())
        elif bias[i] < 0:
            reversal = int((forward[:, 1] >= entry + float(config.break_atr) * atr[i]).any())
        rows.append(
            {
                "asof": data.at[i, "end"],
                "label_end": data.at[i + config.horizon, "end"],
                "barrier_end": data.at[resolution, "end"],
                "y_trend": np.nan if is_ambiguous else int(outcome != 0),
                "y_up": np.nan if is_ambiguous or outcome == 0 else int(outcome > 0),
                "y_break": np.nan if reversal is None else reversal,
                "causal_side": int(bias[i]),
                "barrier": "ambiguous"
                if is_ambiguous
                else ("upper" if outcome == 1 else "lower" if outcome == -1 else "timeout"),
            }
        )
    return LabelSet(rows=pd.DataFrame(rows), ambiguous=ambiguous, censored=censored)


def l1_trend_filter(values: NDArray[np.float64], strength: float = 10.0) -> NDArray[np.float64]:
    """Kim et al. convex l1 trend filtering через OSQP; только auxiliary labels.

    min_x 0.5 ||x-y||² + λ||D²x||₁; epigraph formulation, не heuristic MA.
    Решение на всей серии ретроспективно и NEVER доступно feature builder.
    """
    y = np.asarray(values, dtype=float)
    if y.ndim != 1 or len(y) < 4 or not np.isfinite(y).all() or strength <= 0:
        raise ValueError("l1 filter требует конечный одномерный ряд и λ > 0")
    n, m = len(y), len(y) - 2
    second = sparse.diags(
        [np.ones(m), -2 * np.ones(m), np.ones(m)], [0, 1, 2], shape=(m, n), format="csc"
    )
    identity = sparse.eye(m, format="csc")
    constraints = sparse.vstack(
        [sparse.hstack([second, -identity]), sparse.hstack([-second, -identity])], format="csc"
    )
    quadratic = sparse.block_diag(
        (sparse.eye(n, format="csc"), sparse.csc_matrix((m, m))), format="csc"
    )
    solver = osqp.OSQP()
    solver.setup(
        P=quadratic,
        q=np.r_[-y, np.full(m, strength)],
        A=constraints,
        l=np.full(2 * m, -np.inf),
        u=np.zeros(2 * m),
        verbose=False,
        eps_abs=1e-6,
        eps_rel=1e-6,
        max_iter=20000,
    )
    solution = solver.solve(raise_error=True)
    if solution.info.status_val not in {1, 2} or solution.x is None:
        raise ValueError("l1 trend filtering не сошёлся")
    return np.asarray(solution.x[:n], dtype=float)
