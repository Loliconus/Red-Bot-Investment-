"""MLOps, версионирование датасетов, трекинг экспериментов и мониторинг дрейфа (раздел 7 ТЗ).

Реализует:
1. **Версионирование датасетов (DVC-совместимый манифест)** — детерминированный SHA-256
   отпечаток среза свечей, таймфреймов и схемы признаков для стопроцентной
   воспроизводимости любого эксперимента (раздел 7.1).
2. **Трекинг экспериментов (MLflow-совместимый реестр запусков)** — фиксация параметров,
   метрик Brier Score, ECE, PBO, DSR, White RC, Hansen SPA и важности SHAP (раздел 7.1).
3. **Мониторинг дрейфа признаков (Population Stability Index — PSI)** и деградации
   калибровки в бою с автоматическим триггером переобучения на свежем окне (раздел 7.2).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from core.domain.value_objects import OHLCV


class DriftStatus(StrEnum):
    """Статус стабильности распределения признаков по PSI."""

    STABLE = "stable"  # PSI < 0.10
    WARNING = "warning"  # 0.10 <= PSI < 0.20
    CRITICAL_RETRAIN = "critical_retrain"  # PSI >= 0.20 (требуется переобучение)


@dataclass(frozen=True, slots=True, kw_only=True)
class DatasetVersionManifest:
    """DVC-совместимый паспорт версии обучающего датасета."""

    dataset_id: str
    sha256_digest: str
    instrument_uid: str
    timeframe: str
    bar_count: int
    start_timestamp: datetime
    end_timestamp: datetime
    feature_schema_version: str
    feature_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentRunRecord:
    """MLflow-совместимая запись эксперимента обучения и валидации."""

    run_id: str
    recorded_at: datetime
    dataset_manifest: DatasetVersionManifest
    model_stage: str
    hyperparameters: Mapping[str, str | int | float]
    metrics: Mapping[str, float]
    top_shap_features: Mapping[str, float]
    beats_all_benchmarks: bool
    shadow_mode_ready: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureDriftDiagnostic:
    """Диагностика смещения распределения признака (PSI) и деградации калибровки."""

    overall_status: DriftStatus
    max_psi: float
    mean_psi: float
    psi_by_feature: Mapping[str, float]
    drifted_features: tuple[str, ...]
    live_brier_score: float
    reference_brier_score: float
    should_trigger_retraining: bool
    retraining_reason: str | None


def build_dataset_manifest(
    instrument_uid: str,
    timeframe: str,
    candles: Sequence[OHLCV],
    feature_names: Sequence[str],
    *,
    schema_version: str = "synthetic-v1.0",
) -> DatasetVersionManifest:
    """Строит криптографический паспорт версии датасета (SHA-256)."""
    hasher = hashlib.sha256()
    hasher.update(f"{instrument_uid}|{timeframe}|{schema_version}|".encode())
    for name in sorted(feature_names):
        hasher.update(f"F:{name};".encode())
    for bar in candles:
        payload = (
            f"{bar.timestamp.isoformat()}:{bar.open}:{bar.high}:{bar.low}:{bar.close}:{bar.volume};"
        )
        hasher.update(payload.encode())

    digest = hasher.hexdigest()
    short_id = f"dvc-{instrument_uid.lower()}-{timeframe}-{digest[:12]}"
    start_ts = candles[0].timestamp if candles else datetime.min
    end_ts = candles[-1].timestamp if candles else datetime.min

    return DatasetVersionManifest(
        dataset_id=short_id,
        sha256_digest=digest,
        instrument_uid=instrument_uid,
        timeframe=timeframe,
        bar_count=len(candles),
        start_timestamp=start_ts,
        end_timestamp=end_ts,
        feature_schema_version=schema_version,
        feature_count=len(feature_names),
    )


def compute_feature_psi(
    reference_values: Sequence[float],
    current_values: Sequence[float],
    *,
    n_bins: int = 10,
) -> float:
    """Вычисляет Population Stability Index (PSI) между эталонным и текущим окнами.

    Формула::

        PSI = sum_{b=1..B} (p_cur[b] - p_ref[b]) * ln(p_cur[b] / p_ref[b])

    Интерпретация:
    - ``PSI < 0.10`` — распределение стабильно;
    - ``0.10 <= PSI < 0.20`` — умеренный дрейф;
    - ``PSI >= 0.20`` — критический сдвиг распределения, триггер переобучения.
    """
    n_ref = len(reference_values)
    n_cur = len(current_values)
    if n_ref < 5 or n_cur < 5:
        return 0.0

    sorted_ref = sorted(reference_values)
    # Строим квантильные границы по эталонному (обучающему) распределению
    cut_points: list[float] = []
    for b in range(1, n_bins):
        idx = min(n_ref - 1, int(n_ref * b / n_bins))
        cut_points.append(sorted_ref[idx])

    ref_counts = [0] * n_bins
    cur_counts = [0] * n_bins

    for v in reference_values:
        bin_idx = n_bins - 1
        for b, cut in enumerate(cut_points):
            if v <= cut:
                bin_idx = b
                break
        ref_counts[bin_idx] += 1

    for v in current_values:
        bin_idx = n_bins - 1
        for b, cut in enumerate(cut_points):
            if v <= cut:
                bin_idx = b
                break
        cur_counts[bin_idx] += 1

    psi = 0.0
    eps = 1e-4
    for b in range(n_bins):
        p_ref = max(ref_counts[b] / n_ref, eps)
        p_cur = max(cur_counts[b] / n_cur, eps)
        psi += (p_cur - p_ref) * math.log(p_cur / p_ref)
    return max(0.0, psi)


def evaluate_drift_and_retraining_trigger(
    feature_names: Sequence[str],
    reference_matrix: Sequence[Sequence[float]],
    current_matrix: Sequence[Sequence[float]],
    *,
    live_brier_score: float = 0.18,
    reference_brier_score: float = 0.17,
    psi_warning_threshold: float = 0.10,
    psi_critical_threshold: float = 0.20,
    max_brier_degradation: float = 0.05,
) -> FeatureDriftDiagnostic:
    """Комплексный аудит PSI-дрейфа признаков и деградации калибровки вероятностей."""
    if not feature_names or not reference_matrix or not current_matrix:
        return FeatureDriftDiagnostic(
            overall_status=DriftStatus.STABLE,
            max_psi=0.0,
            mean_psi=0.0,
            psi_by_feature={},
            drifted_features=(),
            live_brier_score=live_brier_score,
            reference_brier_score=reference_brier_score,
            should_trigger_retraining=False,
            retraining_reason=None,
        )

    psi_map: dict[str, float] = {}
    drifted: list[str] = []

    for j, name in enumerate(feature_names):
        ref_col = [row[j] for row in reference_matrix if j < len(row)]
        cur_col = [row[j] for row in current_matrix if j < len(row)]
        psi_val = compute_feature_psi(ref_col, cur_col)
        psi_map[name] = psi_val
        if psi_val >= psi_critical_threshold:
            drifted.append(name)

    max_psi = max(psi_map.values()) if psi_map else 0.0
    mean_psi = (sum(psi_map.values()) / len(psi_map)) if psi_map else 0.0
    brier_delta = live_brier_score - reference_brier_score

    should_retrain = False
    reason: str | None = None
    if max_psi >= psi_critical_threshold:
        status = DriftStatus.CRITICAL_RETRAIN
        should_retrain = True
        reason = f"PSI_CRITICAL_DRIFT({max_psi:.3f}>={psi_critical_threshold:.2f})"
    elif brier_delta >= max_brier_degradation:
        status = DriftStatus.CRITICAL_RETRAIN
        should_retrain = True
        reason = f"BRIER_CALIBRATION_DEGRADATION(+{brier_delta:.3f})"
    elif max_psi >= psi_warning_threshold:
        status = DriftStatus.WARNING
    else:
        status = DriftStatus.STABLE

    return FeatureDriftDiagnostic(
        overall_status=status,
        max_psi=max_psi,
        mean_psi=mean_psi,
        psi_by_feature=psi_map,
        drifted_features=tuple(drifted),
        live_brier_score=live_brier_score,
        reference_brier_score=reference_brier_score,
        should_trigger_retraining=should_retrain,
        retraining_reason=reason,
    )


def build_experiment_record(
    *,
    recorded_at: datetime,
    dataset_manifest: DatasetVersionManifest,
    model_stage: str,
    hyperparameters: Mapping[str, str | int | float],
    metrics: Mapping[str, float],
    top_shap_features: Mapping[str, float],
    strategy_return_pct: Decimal,
    benchmark_returns_pct: Sequence[Decimal],
    pbo_probability: float,
    dsr_probability: float,
) -> ExperimentRunRecord:
    """Создаёт запись эксперимента с проверкой критерия допуска к Shadow Mode / Sandbox."""
    beats_all = all(strategy_return_pct >= b_ret for b_ret in benchmark_returns_pct)
    # Допуск к теневому прогону / песочнице T-Invest: PBO < 0.50 и DSR > 0.50
    shadow_ready = beats_all and pbo_probability < 0.50 and dsr_probability >= 0.50
    run_hash = hashlib.sha256(
        f"{dataset_manifest.sha256_digest}|{recorded_at.isoformat()}|{model_stage}".encode()
    ).hexdigest()[:10]

    return ExperimentRunRecord(
        run_id=f"exp-{run_hash}",
        recorded_at=recorded_at,
        dataset_manifest=dataset_manifest,
        model_stage=model_stage,
        hyperparameters=dict(hyperparameters),
        metrics=dict(metrics),
        top_shap_features=dict(top_shap_features),
        beats_all_benchmarks=beats_all,
        shadow_mode_ready=shadow_ready,
    )
