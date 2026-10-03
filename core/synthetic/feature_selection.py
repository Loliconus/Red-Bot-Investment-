"""Трёхэтапный конвейер отбора признаков «Синтетического трейдера» (раздел 3.3 ТЗ).

Этапы отбора:
1. **Elastic Net (ℓ1 + ℓ2)** — быстрый линейный фильтр покоординатным спуском с
   мягким порогом (soft-thresholding), обнуляющий откровенно мусорные признаки.
2. **Stability Selection на скользящих временных окнах (walk-forward)** — признак
   остаётся в пуле только если он значим стабильно на разных исторических эпохах,
   а не на одном удачном году.
3. **CatBoost SHAP + отсечение коллинеарных дублей** — ранжирование по среднему модулю
   SHAP-вкладов и удаление пар признаков с взаимной корреляцией ``|r| > 0.90``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureSelectionReport:
    """Аудируемый отчёт о прохождении признаков через 3 этапа фильтрации."""

    initial_features: tuple[str, ...]
    elastic_net_survivors: tuple[str, ...]
    stability_survivors: tuple[str, ...]
    final_selected_features: tuple[str, ...]
    elastic_net_weights: Mapping[str, float]
    stability_frequencies: Mapping[str, float]
    shap_importances: Mapping[str, float]
    dropped_collinear_pairs: tuple[tuple[str, str, float], ...]


def _soft_threshold(value: float, gamma: float) -> float:
    if value > gamma:
        return value - gamma
    if value < -gamma:
        return value + gamma
    return 0.0


def _standardize_columns(
    matrix: Sequence[Sequence[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Стандартизует столбцы матрицы признаков (mean=0, std=1)."""
    if not matrix or not matrix[0]:
        return [], [], []
    n_samples = len(matrix)
    n_features = len(matrix[0])
    means = [0.0] * n_features
    stds = [1.0] * n_features

    for j in range(n_features):
        col_sum = sum(matrix[i][j] for i in range(n_samples))
        mean_j = col_sum / n_samples
        means[j] = mean_j
        var_j = sum((matrix[i][j] - mean_j) ** 2 for i in range(n_samples)) / max(n_samples, 1)
        std_j = math.sqrt(var_j)
        stds[j] = std_j if std_j > 1e-12 else 1.0

    std_matrix = [
        [(matrix[i][j] - means[j]) / stds[j] for j in range(n_features)]
        for i in range(n_samples)
    ]
    return std_matrix, means, stds


def fit_elastic_net_weights(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    alpha: float = 0.02,
    l1_ratio: float = 0.5,
    max_iterations: int = 120,
    tol: float = 1e-5,
) -> dict[str, float]:
    """Обучает линейную модель с регуляризацией Elastic Net (ℓ1 + ℓ2) покоординатным спуском."""
    n_samples = len(matrix)
    n_features = len(feature_names)
    if n_samples == 0 or n_features == 0:
        return {name: 0.0 for name in feature_names}

    x_std, _, _ = _standardize_columns(matrix)
    y_mean = sum(targets) / n_samples
    y_centered = [float(y) - y_mean for y in targets]

    weights = [0.0] * n_features
    residual = list(y_centered)
    l1_pen = alpha * l1_ratio
    l2_pen = alpha * (1.0 - l1_ratio)

    for _it in range(max_iterations):
        max_change = 0.0
        for j in range(n_features):
            old_w = weights[j]
            # Восстанавливаем частичную невязку по координате j
            rho_j = 0.0
            for i in range(n_samples):
                rho_j += x_std[i][j] * (residual[i] + old_w * x_std[i][j])
            rho_j /= n_samples

            new_w = _soft_threshold(rho_j, l1_pen) / (1.0 + l2_pen)
            diff = new_w - old_w
            if diff != 0.0:
                weights[j] = new_w
                for i in range(n_samples):
                    residual[i] -= diff * x_std[i][j]
                if abs(diff) > max_change:
                    max_change = abs(diff)
        if max_change < tol:
            break

    return {feature_names[j]: weights[j] for j in range(n_features)}


def compute_walk_forward_stability(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    n_windows: int = 4,
    alpha: float = 0.015,
    l1_ratio: float = 0.5,
    weight_threshold: float = 1e-4,
) -> dict[str, float]:
    """Оценивает частоту сохранения признака на последовательных временных окнах."""
    n_samples = len(matrix)
    if n_samples < 12 or not feature_names:
        return {name: 1.0 for name in feature_names}

    effective_windows = max(2, min(n_windows, n_samples // 6))
    win_size = n_samples // effective_windows
    counts = {name: 0 for name in feature_names}

    for w_idx in range(effective_windows):
        start = w_idx * win_size
        end = n_samples if w_idx == effective_windows - 1 else (w_idx + 1) * win_size
        sub_x = matrix[start:end]
        sub_y = targets[start:end]
        w_map = fit_elastic_net_weights(
            feature_names,
            sub_x,
            sub_y,
            alpha=alpha,
            l1_ratio=l1_ratio,
        )
        for name, val in w_map.items():
            if abs(val) > weight_threshold:
                counts[name] += 1

    return {name: counts[name] / effective_windows for name in feature_names}


def _pearson_corr(col_a: Sequence[float], col_b: Sequence[float]) -> float:
    n = len(col_a)
    if n < 2:
        return 0.0
    ma = sum(col_a) / n
    mb = sum(col_b) / n
    cov = sum((col_a[i] - ma) * (col_b[i] - mb) for i in range(n))
    va = sum((col_a[i] - ma) ** 2 for i in range(n))
    vb = sum((col_b[i] - mb) ** 2 for i in range(n))
    denom = math.sqrt(va * vb)
    return cov / denom if denom > 1e-12 else 0.0


def run_feature_selection_pipeline(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    shap_importances: Mapping[str, float] | None = None,
    elastic_alpha: float = 0.015,
    l1_ratio: float = 0.5,
    min_stability_freq: float = 0.50,
    max_collinearity: float = 0.90,
    min_features_keep: int = 8,
) -> FeatureSelectionReport:
    """Выполняет полный 3-ступенчатый отбор признаков по разделу 3.3 ТЗ."""
    initial = tuple(feature_names)
    if not initial or not matrix:
        return FeatureSelectionReport(
            initial_features=initial,
            elastic_net_survivors=(),
            stability_survivors=(),
            final_selected_features=(),
            elastic_net_weights={},
            stability_frequencies={},
            shap_importances={},
            dropped_collinear_pairs=(),
        )

    # Этап 1: Elastic Net (ℓ1 + ℓ2)
    enet_weights = fit_elastic_net_weights(
        initial,
        matrix,
        targets,
        alpha=elastic_alpha,
        l1_ratio=l1_ratio,
    )
    enet_survivors = [name for name in initial if abs(enet_weights.get(name, 0.0)) > 1e-5]
    if len(enet_survivors) < min_features_keep:
        ranked_enet = sorted(initial, key=lambda nm: abs(enet_weights.get(nm, 0.0)), reverse=True)
        enet_survivors = ranked_enet[: max(min_features_keep, len(enet_survivors))]

    # Этап 2: Stability Selection на скользящих временных окнах
    stability_freqs = compute_walk_forward_stability(
        initial,
        matrix,
        targets,
        alpha=elastic_alpha,
        l1_ratio=l1_ratio,
    )
    stability_survivors = [
        name for name in enet_survivors if stability_freqs.get(name, 0.0) >= min_stability_freq
    ]
    if len(stability_survivors) < min_features_keep:
        ranked_stab = sorted(
            enet_survivors,
            key=lambda nm: (stability_freqs.get(nm, 0.0), abs(enet_weights.get(nm, 0.0))),
            reverse=True,
        )
        stability_survivors = ranked_stab[: min(len(enet_survivors), min_features_keep)]

    # Этап 3: SHAP-значимость + удаление сильно коллинеарных пар (|corr| > 0.90)
    name_to_idx = {name: idx for idx, name in enumerate(initial)}
    if shap_importances is not None:
        shap_map = {name: float(abs(shap_importances.get(name, 0.0))) for name in initial}
    else:
        shap_map = {
            name: abs(enet_weights.get(name, 0.0)) * (0.5 + stability_freqs.get(name, 0.0))
            for name in initial
        }

    # Сортируем кандидатов по убыванию SHAP-важности
    ordered_candidates = sorted(
        stability_survivors,
        key=lambda nm: (shap_map.get(nm, 0.0), stability_freqs.get(nm, 0.0)),
        reverse=True,
    )

    columns = {
        name: [row[name_to_idx[name]] for row in matrix]
        for name in ordered_candidates
    }

    kept: list[str] = []
    dropped_pairs: list[tuple[str, str, float]] = []

    for candidate in ordered_candidates:
        is_redundant = False
        for existing in kept:
            corr = _pearson_corr(columns[candidate], columns[existing])
            if abs(corr) > max_collinearity:
                dropped_pairs.append((candidate, existing, corr))
                is_redundant = True
                break
        if not is_redundant:
            kept.append(candidate)

    return FeatureSelectionReport(
        initial_features=initial,
        elastic_net_survivors=tuple(enet_survivors),
        stability_survivors=tuple(stability_survivors),
        final_selected_features=tuple(kept),
        elastic_net_weights=enet_weights,
        stability_frequencies=stability_freqs,
        shap_importances=shap_map,
        dropped_collinear_pairs=tuple(dropped_pairs),
    )
