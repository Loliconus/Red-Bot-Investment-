"""Временные folds из purgedcv, сгруппированные по моменту для всей корзины."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from purgedcv import CombinatorialPurgedCV, PurgedKFold, WalkForwardSplit, audit_splitter
from purgedcv.diagnostics import assert_no_temporal_leakage

from synthetic_trader.config import ExperimentConfig


@dataclass(slots=True)
class TemporalFold:
    train: NDArray[np.int64]
    test: NDArray[np.int64]
    audit: dict[str, Any]


def time_axis(rows: pd.DataFrame) -> pd.DataFrame:
    # Label spans всех 3 heads и всех assets; одинаковое t не разрывается fold-ом.
    return rows.groupby("asof", sort=True)["label_end"].max().reset_index()


def outer_folds(
    rows: pd.DataFrame,
    config: ExperimentConfig,
    *,
    mode: Literal["walk_forward", "cpcv", "purged_kfold"] = "walk_forward",
) -> list[TemporalFold]:
    axis = time_axis(rows)
    if len(axis) < 300:
        raise ValueError("Недостаточно закрытых размеченных моментов для temporal validation")
    kwargs = {
        "prediction_times": axis["asof"],
        "evaluation_times": axis["label_end"],
        "embargo_observations": config.embargo_bars,
    }
    cv: Any
    if mode == "walk_forward":
        cv = WalkForwardSplit(
            n_splits=config.folds, test_size=max(40, int(len(axis) * 0.45 / config.folds)), **kwargs
        )
    elif mode == "purged_kfold":
        cv = PurgedKFold(n_splits=config.folds, **kwargs)
    else:
        cv = CombinatorialPurgedCV(n_splits=config.cpcv_groups, n_test_groups=2, **kwargs)
    placeholder = np.zeros((len(axis), 1))
    audit = audit_splitter(cv, placeholder)
    result: list[TemporalFold] = []
    for index, (train_times, test_times) in enumerate(cv.split(placeholder)):
        assert_no_temporal_leakage(train_times, test_times, axis["asof"], axis["label_end"])
        train = np.flatnonzero(rows["asof"].isin(axis.iloc[train_times]["asof"]).to_numpy()).astype(
            np.int64
        )
        test = np.flatnonzero(rows["asof"].isin(axis.iloc[test_times]["asof"]).to_numpy()).astype(
            np.int64
        )
        if len(train) < 200 or not len(test):
            raise ValueError("Purge/embargo оставили недостаточно train/test наблюдений")
        record = audit.iloc[index].to_dict()
        record["mode"] = mode
        record["same_time_assets_grouped"] = True
        if (
            mode == "walk_forward"
            and rows.iloc[train]["label_end"].max() > rows.iloc[test]["asof"].min()
        ):
            raise ValueError("Walk-forward обучается на ещё не разрешившихся метках")
        result.append(TemporalFold(train=train, test=test, audit=record))
    return result


@dataclass(slots=True)
class InnerSplit:
    fit: pd.DataFrame
    validation: pd.DataFrame
    calibration: pd.DataFrame
    audit: dict[str, Any]


def inner_split(rows: pd.DataFrame) -> InnerSplit:
    """Fit → early-stop/SHAP validation → неприкосновенная calibration.

    Два независимых purge по ПОЛНОМУ label span. Никакой общей нормализации.
    В CPCV train может быть несвязным; внутренние куски всё равно хронологичны.
    """
    times = rows["asof"].drop_duplicates().sort_values().reset_index(drop=True)
    if len(times) < 140:
        raise ValueError("Внутренний fold слишком короткий для fit / validation / calibration")
    val_start, cal_start = times.iloc[int(len(times) * 0.65)], times.iloc[int(len(times) * 0.82)]
    fit = rows.loc[(rows["asof"] < val_start) & (rows["label_end"] <= val_start)].copy()
    validation = rows.loc[
        (rows["asof"] >= val_start) & (rows["asof"] < cal_start) & (rows["label_end"] <= cal_start)
    ].copy()
    calibration = rows.loc[rows["asof"] >= cal_start].copy()
    if min(len(fit), len(validation), len(calibration)) < 30:
        raise ValueError("После внутреннего purging осталось <30 наблюдений")
    return InnerSplit(
        fit=fit,
        validation=validation,
        calibration=calibration,
        audit={
            "fit_rows": len(fit),
            "validation_rows": len(validation),
            "calibration_rows": len(calibration),
            "fit_end": fit["label_end"].max(),
            "validation_start": val_start,
            "validation_end": validation["label_end"].max(),
            "calibration_start": cal_start,
            "purged_rows": len(rows) - len(fit) - len(validation) - len(calibration),
            "calibration_used_for_selection": False,
        },
    )
