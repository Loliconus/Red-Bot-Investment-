"""Elastic-net на временных блоках, затем train-only корреляционная чистка."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from synthetic_trader.config import ExperimentConfig

TARGETS = ("y_trend", "y_up", "y_break")


@dataclass(slots=True)
class SelectionResult:
    columns: list[str]
    frequencies: dict[str, float]
    blocks: list[dict[str, object]]
    fallback: bool


def stability_select(
    rows: pd.DataFrame, columns: list[str], config: ExperimentConfig
) -> SelectionResult:
    """Четыре overlapping chronological windows; preprocessing fit внутри каждого."""
    times = rows["asof"].drop_duplicates().sort_values().reset_index(drop=True)
    selected = np.zeros(len(columns))
    magnitudes = np.zeros(len(columns))
    blocks: list[dict[str, object]] = []
    window = max(60, len(times) // 2)
    for end in np.linspace(window, len(times), 4, dtype=int):
        block = rows.loc[
            rows["asof"].isin(times.iloc[max(0, end - window) : end])
            & (rows["label_end"] <= times.iloc[end - 1])
        ]
        matrix = block[columns].to_numpy(dtype=float)
        imputer = SimpleImputer(strategy="median", keep_empty_features=True).fit(matrix)
        scaler = StandardScaler().fit(imputer.transform(matrix))
        values = scaler.transform(imputer.transform(matrix))
        chosen = np.zeros(len(columns), dtype=bool)
        total_coef = np.zeros(len(columns))
        fitted = 0
        for target in TARGETS:
            valid = block[target].notna().to_numpy()
            labels = block.loc[valid, target].to_numpy(dtype=int)
            if len(labels) < 30 or len(np.unique(labels)) != 2:
                continue
            selector = LogisticRegression(
                solver="saga",
                l1_ratio=0.65,
                C=0.5,
                max_iter=1200,
                tol=0.002,
                random_state=config.seed,
                class_weight="balanced",
            )
            selector.fit(values[valid], labels)
            coef = np.abs(selector.coef_[0])
            chosen |= coef > 1e-5
            total_coef += coef
            fitted += 1
        selected += chosen
        magnitudes += total_coef
        blocks.append(
            {
                "start": block["asof"].min(),
                "end": block["asof"].max(),
                "rows": len(block),
                "selected": int(chosen.sum()),
                "heads": fitted,
            }
        )
    frequency = selected / len(blocks)
    ordered = sorted(range(len(columns)), key=lambda i: (-frequency[i], -magnitudes[i], columns[i]))
    stable = [columns[i] for i in ordered if frequency[i] >= config.stability_threshold][
        : config.max_features
    ]
    fallback = not stable
    if fallback:
        # Явный компактный exploratory fallback, НИКОГДА не выдаётся за stability selection.
        stable = [
            c
            for c in (
                "return_1",
                "rv_20",
                "atr_pct",
                "ema_dist_20",
                "volume_z_20",
                "regime_trend",
                "regime_panic",
            )
            if c in columns
        ]
    return SelectionResult(
        columns=stable,
        frequencies=dict(zip(columns, frequency.tolist(), strict=True)),
        blocks=blocks,
        fallback=fallback,
    )


def shap_correlation_prune(
    rows: pd.DataFrame, columns: list[str], importance: dict[str, float]
) -> list[str]:
    """Ранжирование по native SHAP, |corr|>0.9 отбрасывается на FIT, не test."""
    if not columns:
        raise ValueError("Нет признаков для SHAP pruning")
    order = sorted(columns, key=lambda c: (-importance.get(c, 0.0), c))
    corr = rows[order].corr().abs()
    maximum = max(importance.values(), default=0.0)
    kept: list[str] = []
    for col in order:
        if maximum and importance.get(col, 0.0) < maximum * 0.01:
            continue
        if all(not np.isfinite(corr.at[col, old]) or corr.at[col, old] <= 0.9 for old in kept):
            kept.append(col)
    return kept or order[:1]
