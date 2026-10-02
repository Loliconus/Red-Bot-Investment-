"""Три CatBoost головы, отдельная isotonic calibration и native SHAP.

UP обучается / калибруется / оценивается только на y_trend=1. Все решения
селектора, early stopping и SHAP принимаются ДО calibration и outer test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, Pool
from numpy.typing import NDArray
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.regime import REGIME_FEATURES, FilteredMarkovRegime
from synthetic_trader.selection import TARGETS, shap_correlation_prune, stability_select
from synthetic_trader.validation import inner_split

HEAD_NAMES = {"y_trend": "trend", "y_up": "up", "y_break": "break"}


@dataclass(slots=True)
class BinaryHead:
    model: Any
    prior: float
    calibration_x: NDArray[np.float64] | None = None
    calibration_y: NDArray[np.float64] | None = None
    calibration_status: str = "pending"

    def raw_predict(self, values: pd.DataFrame) -> NDArray[np.float64]:
        if self.model is None:
            return np.full(len(values), self.prior)
        return np.asarray(self.model.predict_proba(values)[:, 1], dtype=float)

    def predict(self, values: pd.DataFrame) -> NDArray[np.float64]:
        raw = self.raw_predict(values)
        if self.calibration_x is None or self.calibration_y is None:
            return raw
        return np.clip(np.interp(raw, self.calibration_x, self.calibration_y), 0, 1)

    def calibrate(self, values: pd.DataFrame, labels: NDArray[np.int64]) -> None:
        if self.model is None:
            self.calibration_status = "unavailable_single_class_fit"
            return
        if len(labels) < 30 or len(np.unique(labels)) < 2:
            self.calibration_status = "unavailable_insufficient_calibration"
            return
        raw = self.raw_predict(values)
        if np.ptp(raw) < 1e-8:
            self.calibration_status = "unavailable_constant_predictions"
            return
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(raw, labels)
        self.calibration_x = np.asarray(calibrator.X_thresholds_, dtype=float)
        self.calibration_y = np.asarray(calibrator.y_thresholds_, dtype=float)
        self.calibration_status = "isotonic_separate_temporal_slice"


def _fit_heads(
    fit: pd.DataFrame, validation: pd.DataFrame, columns: list[str], config: ExperimentConfig
) -> dict[str, BinaryHead]:
    heads: dict[str, BinaryHead] = {}
    for target in TARGETS:
        valid = fit[target].notna()
        labels = fit.loc[valid, target].to_numpy(dtype=int)
        if not len(labels):
            raise ValueError(f"Нет обучающих меток {target}")
        prior = float((labels.sum() + 1) / (len(labels) + 2))
        if len(np.unique(labels)) < 2:
            heads[target] = BinaryHead(model=None, prior=prior)
            continue
        model = CatBoostClassifier(
            iterations=config.iterations,
            depth=config.depth,
            learning_rate=config.learning_rate,
            l2_leaf_reg=config.l2_leaf_reg,
            random_strength=1.0,
            auto_class_weights="Balanced",
            loss_function="Logloss",
            eval_metric="Logloss",
            random_seed=config.seed,
            thread_count=2,
            has_time=True,
            allow_writing_files=False,
            verbose=False,
        )
        validation_valid = validation[target].notna()
        eval_set = None
        if validation_valid.sum() >= 20 and validation.loc[validation_valid, target].nunique() == 2:
            eval_set = (
                validation.loc[validation_valid, columns],
                validation.loc[validation_valid, target].astype(int),
            )
        model.fit(
            fit.loc[valid, columns],
            labels,
            eval_set=eval_set,
            early_stopping_rounds=config.early_stopping_rounds if eval_set else None,
            use_best_model=eval_set is not None,
            verbose=False,
        )
        heads[target] = BinaryHead(model=model, prior=prior)
    return heads


def _mean_validation_brier(
    heads: dict[str, BinaryHead], rows: pd.DataFrame, columns: list[str]
) -> float:
    scores: list[float] = []
    for target, head in heads.items():
        valid = rows[target].notna()
        if valid.any():
            scores.append(
                float(
                    brier_score_loss(
                        rows.loc[valid, target].astype(int),
                        head.raw_predict(rows.loc[valid, columns]),
                    )
                )
            )
    return float(np.mean(scores)) if scores else float("inf")


def reliability(
    labels: NDArray[np.int64], probability: NDArray[np.float64]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    bucket = np.minimum((probability * 10).astype(int), 9)
    for i in range(10):
        mask = bucket == i
        result.append(
            {
                "lower": i / 10,
                "upper": (i + 1) / 10,
                "count": int(mask.sum()),
                "predicted": float(probability[mask].mean()) if mask.any() else None,
                "observed": float(labels[mask].mean()) if mask.any() else None,
            }
        )
    return result


@dataclass(slots=True)
class ProbabilityBundle:
    columns: list[str]
    heads: dict[str, BinaryHead]
    regime: FilteredMarkovRegime
    audit: dict[str, Any]
    reference_bins: dict[str, list[float]]

    def predict(self, history: pd.DataFrame) -> pd.DataFrame:
        context = self.regime.transform(history)
        result = context[
            [
                c
                for c in ("asof", "symbol", "atr", "rv_20", "label_end", *TARGETS, *REGIME_FEATURES)
                if c in context
            ]
        ].copy()
        for target, head in self.heads.items():
            result[f"p_{HEAD_NAMES[target]}"] = head.predict(context[self.columns])
            result[f"raw_{HEAD_NAMES[target]}"] = head.raw_predict(context[self.columns])
            result[f"prior_{HEAD_NAMES[target]}"] = head.prior
        result["regime"] = context[list(REGIME_FEATURES)].idxmax(axis=1).str.removeprefix("regime_")
        return result

    def evaluate(self, predicted: pd.DataFrame) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for target in TARGETS:
            name = HEAD_NAMES[target]
            valid = predicted[target].notna()
            labels = predicted.loc[valid, target].to_numpy(dtype=int)
            probability = predicted.loc[valid, f"p_{name}"].to_numpy(dtype=float)
            raw = predicted.loc[valid, f"raw_{name}"].to_numpy(dtype=float)
            metrics[name] = {
                "samples": len(labels),
                "positives": int(labels.sum()),
                "brier": float(brier_score_loss(labels, probability)) if len(labels) else None,
                "raw_brier": float(brier_score_loss(labels, raw)) if len(labels) else None,
                "constant_train_brier": float(
                    brier_score_loss(labels, predicted.loc[valid, f"prior_{name}"].to_numpy())
                )
                if len(labels)
                else None,
                "calibration": self.heads[target].calibration_status,
                "reliability": reliability(labels, probability) if len(labels) else [],
                "conditional_on_trend": target == "y_up",
            }
        return metrics

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        metadata: dict[str, Any] = {
            "columns": self.columns,
            "regime": self.regime.to_dict(),
            "audit": self.audit,
            "reference_bins": self.reference_bins,
            "heads": {},
        }
        for target, head in self.heads.items():
            if head.model is not None:
                head.model.save_model(str(path / f"{target}.cbm"))
            metadata["heads"][target] = {
                "has_model": head.model is not None,
                "prior": head.prior,
                "calibration_x": head.calibration_x.tolist()
                if head.calibration_x is not None
                else None,
                "calibration_y": head.calibration_y.tolist()
                if head.calibration_y is not None
                else None,
                "calibration_status": head.calibration_status,
            }
        # JSON+CBM, не pickle/joblib. Пути загрузки только из локального registry.
        from synthetic_trader.storage import write_json

        write_json(path / "bundle.json", metadata)

    @classmethod
    def load(cls, path: Path) -> Self:
        metadata = json.loads((path / "bundle.json").read_text())
        heads: dict[str, BinaryHead] = {}
        for target, data in metadata["heads"].items():
            model = None
            if data["has_model"]:
                model = CatBoostClassifier()
                model.load_model(str(path / f"{target}.cbm"))
            heads[target] = BinaryHead(
                model=model,
                prior=data["prior"],
                calibration_x=np.asarray(data["calibration_x"])
                if data["calibration_x"] is not None
                else None,
                calibration_y=np.asarray(data["calibration_y"])
                if data["calibration_y"] is not None
                else None,
                calibration_status=data["calibration_status"],
            )
        return cls(
            columns=metadata["columns"],
            heads=heads,
            regime=FilteredMarkovRegime.from_dict(metadata["regime"]),
            audit=metadata["audit"],
            reference_bins=metadata["reference_bins"],
        )


def fit_bundle(
    train_rows: pd.DataFrame, feature_columns: list[str], config: ExperimentConfig
) -> ProbabilityBundle:
    split = inner_split(train_rows)
    regime = FilteredMarkovRegime.fit(split.fit, config.seed)
    context = regime.transform(train_rows)
    fit = context.merge(split.fit[["asof", "symbol"]], on=["asof", "symbol"], validate="one_to_one")
    validation = context.merge(
        split.validation[["asof", "symbol"]], on=["asof", "symbol"], validate="one_to_one"
    )
    calibration = context.merge(
        split.calibration[["asof", "symbol"]], on=["asof", "symbol"], validate="one_to_one"
    )
    candidates = [*feature_columns, *REGIME_FEATURES]
    selection = stability_select(fit, candidates, config)
    columns = selection.columns
    heads = _fit_heads(fit, validation, columns, config)
    importance = dict.fromkeys(columns, 0.0)
    for target, head in heads.items():
        if head.model is None:
            continue
        sample = fit.loc[fit[target].notna(), columns].iloc[-256:]
        shap = np.asarray(head.model.get_feature_importance(Pool(sample), type="ShapValues"))[
            :, :-1
        ]
        for col, value in zip(columns, np.abs(shap).mean(axis=0), strict=True):
            importance[col] += float(value) / len(heads)
    pruned = shap_correlation_prune(fit, columns, importance)
    before = _mean_validation_brier(heads, validation, columns)
    after = before
    accepted = False
    if len(pruned) < len(columns):
        slim_heads = _fit_heads(fit, validation, pruned, config)
        after = _mean_validation_brier(slim_heads, validation, pruned)
        # Не смотрим на outer test, чтобы «доказать» полезность чистки.
        if after <= before + 0.002:
            columns, heads, accepted = pruned, slim_heads, True
    for target, head in heads.items():
        valid = calibration[target].notna()
        head.calibrate(
            calibration.loc[valid, columns], calibration.loc[valid, target].to_numpy(dtype=int)
        )
    bins: dict[str, list[float]] = {}
    for col in columns:
        values = fit[col].to_numpy(dtype=float)
        bounds = np.unique(np.quantile(values[np.isfinite(values)], np.linspace(0, 1, 11)))
        bins[col] = bounds.tolist()
    audit = {
        "inner_split": split.audit,
        "candidate_features": len(candidates),
        "stable_features": len(selection.columns),
        "selected_features": len(columns),
        "stability_frequencies": selection.frequencies,
        "stability_blocks": selection.blocks,
        "exploratory_fallback": selection.fallback,
        "shap": importance,
        "pruning_accepted": accepted,
        "validation_brier_before": before,
        "validation_brier_after": after,
        "regime_converged": regime.converged,
        "calibration_rows": len(calibration),
        "test_used_for_fit_or_selection": False,
        "tree_counts": {
            target: head.model.tree_count_ if head.model else 0 for target, head in heads.items()
        },
        "calibration_status": {target: head.calibration_status for target, head in heads.items()},
    }
    return ProbabilityBundle(
        columns=columns, heads=heads, regime=regime, audit=audit, reference_bins=bins
    )


def bundle_hash(directory: Path) -> str:
    """CBM + regime/calibration/selection state; никаких pickle / произвольного кода."""
    from synthetic_trader.storage import digest, file_digest

    names = ("bundle.json", *(f"{target}.cbm" for target in HEAD_NAMES))
    # Single-class heads have no CBM; absence is part of the fingerprint.
    return digest(
        {
            name: file_digest(directory / name) if (directory / name).is_file() else None
            for name in names
        }
    )
