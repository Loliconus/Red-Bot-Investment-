"""Gaussian emissions + Markov transitions, исключительно FORWARD filtering.

Дешёвый regime baseline, не утверждение, что латентные состояния истинны.
Не использует Viterbi, smoothed posterior или параметры, оценённые на test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

REGIME_INPUTS = ("market_return_1", "market_rv_20", "market_ema_dist_20")
REGIME_FEATURES = ("regime_trend", "regime_range", "regime_panic")


@dataclass(slots=True)
class FilteredMarkovRegime:
    mean: NDArray[np.float64]
    scale: NDArray[np.float64]
    centers: NDArray[np.float64]
    variances: NDArray[np.float64]
    transition: NDArray[np.float64]
    prior: NDArray[np.float64]
    order: tuple[int, int, int]
    converged: bool

    @classmethod
    def fit(cls, rows: pd.DataFrame, seed: int) -> Self:
        unique = rows.drop_duplicates("asof").sort_values("asof")
        values = unique[list(REGIME_INPUTS)].to_numpy(dtype=float)
        if len(values) < 80 or not np.isfinite(values).all():
            raise ValueError("Regime fit требует >=80 конечных anchor observations")
        scaler = StandardScaler().fit(values)
        standardized = scaler.transform(values)
        mixture = GaussianMixture(
            n_components=3,
            covariance_type="diag",
            random_state=seed,
            reg_covar=0.01,
            n_init=2,
            max_iter=150,
        ).fit(standardized)
        states = mixture.predict(standardized)
        transition = np.ones((3, 3))  # Dirichlet smoothing, нет zero-prob transitions
        delta = unique["asof"].diff()
        typical_gap = delta.median()
        for i in range(1, len(states)):
            if delta.iloc[i] <= typical_gap * 2:
                transition[states[i - 1], states[i]] += 1
        transition /= transition.sum(axis=1, keepdims=True)
        raw_centers = mixture.means_ * scaler.scale_ + scaler.mean_
        panic = int(np.argmax(raw_centers[:, 1]))
        others = [i for i in range(3) if i != panic]
        trend = max(others, key=lambda i: abs(raw_centers[i, 2]))
        ranging = next(i for i in others if i != trend)
        return cls(
            mean=np.asarray(scaler.mean_),
            scale=np.asarray(scaler.scale_),
            centers=np.asarray(mixture.means_),
            variances=np.asarray(mixture.covariances_),
            transition=transition,
            prior=np.asarray(mixture.weights_),
            order=(trend, ranging, panic),
            converged=bool(mixture.converged_),
        )

    def transform(self, rows: pd.DataFrame) -> pd.DataFrame:
        unique = rows.drop_duplicates("asof").sort_values("asof")
        values = unique[list(REGIME_INPUTS)].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Regime serving получил неконечные признаки")
        z = (values - self.mean) / self.scale
        log_emission = -0.5 * (
            np.log(2 * np.pi * self.variances).sum(axis=1)[None, :]
            + ((z[:, None, :] - self.centers[None, :, :]) ** 2 / self.variances[None, :, :]).sum(
                axis=2
            )
        )
        emission = np.exp(log_emission - log_emission.max(axis=1, keepdims=True))
        output = np.empty((len(unique), 3))
        alpha = self.prior.copy()
        for i, likelihood in enumerate(emission):
            if i:
                alpha = alpha @ self.transition
            alpha *= likelihood
            alpha /= alpha.sum()
            output[i] = alpha[list(self.order)]
        context = pd.DataFrame(output, columns=list(REGIME_FEATURES))
        context["asof"] = unique["asof"].to_numpy()
        return rows.merge(context, on="asof", how="left", validate="many_to_one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "centers": self.centers.tolist(),
            "variances": self.variances.tolist(),
            "transition": self.transition.tolist(),
            "prior": self.prior.tolist(),
            "order": list(self.order),
            "converged": self.converged,
            "method": "GaussianMixture emissions + empirical Markov transitions / causal forward filter",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            mean=np.asarray(data["mean"]),
            scale=np.asarray(data["scale"]),
            centers=np.asarray(data["centers"]),
            variances=np.asarray(data["variances"]),
            transition=np.asarray(data["transition"]),
            prior=np.asarray(data["prior"]),
            order=tuple(data["order"]),
            converged=bool(data["converged"]),
        )
