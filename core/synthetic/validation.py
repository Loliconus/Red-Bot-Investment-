"""Валидация временных рядов и защита от подгонки бэктеста (раздел 5 ТЗ).

Реализует:
1. **Замороженный финальный период (Frozen Holdout)** — отсечение последних 6–12 месяцев
   от любых процедур обучения и подбора гиперпараметров (раздел 5.4).
2. **Purged K-Fold + Embargo** (López de Prado, 2018) — очистка обучающей выборки от
   наблюдений, чьи окна разметки ``[t_start, t_end]`` пересекаются с тестовым фолдом,
   плюс защитный буфер Embargo после тестового блока (раздел 5.1).
3. **Combinatorial Purged Cross-Validation (CPCV)** — генерация ``C(N, k)`` комбинаторных
   фолдов с реконструкцией полных вневыборочных путей бэктеста (раздел 5.2).
4. **Probability of Backtest Overfitting (PBO)** через CSCV (Bailey et al., 2017).
5. **Deflated Sharpe Ratio (DSR)** с поправкой на число попыток ``N_trials``, асимметрию
   и эксцесс доходностей (Bailey & López de Prado, 2014).
6. **White's Reality Check (2000)** и **Hansen's SPA test (2005)** на стационарном
   блочном бутстрэпе (раздел 5.3).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

EULER_MASCHERONI: float = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Аппроксимация квантиля стандартного нормального распределения (Acklam)."""
    pc = min(max(p, 1e-9), 1.0 - 1e-9)
    # Рациональная аппроксимация Абрамовица — Стеган / Акклама
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow = 0.02425
    phigh = 1.0 - plow
    if pc < plow:
        q = math.sqrt(-2.0 * math.log(pc))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if pc <= phigh:
        q = pc - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - pc))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgedFoldSplit:
    """Один фолд Purged K-Fold или CPCV с явным учётом вычищенных и эмбарго-индексов."""

    fold_id: int
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_count: int
    embargoed_count: int
    test_group_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenHoldoutPartition:
    """Разделение выборки на рабочий контур разработки и замороженный финальный Holdout."""

    dev_indices: tuple[int, ...]
    holdout_indices: tuple[int, ...]
    cutoff_timestamp: datetime
    holdout_months: float


@dataclass(frozen=True, slots=True, kw_only=True)
class OverfittingAuditReport:
    """Итоговый отчёт статистической верификации против переобучения (раздел 5 ТЗ)."""

    cpcv_n_splits: int
    cpcv_n_paths: int
    cpcv_mean_oos_sharpe: float
    cpcv_std_oos_sharpe: float
    cpcv_path_sharpes: tuple[float, ...]
    pbo_probability: float
    pbo_logit_median: float
    observed_sharpe: float
    deflated_sharpe_ratio: float
    expected_max_null_sharpe: float
    n_trials_tested: int
    white_reality_check_pvalue: float
    hansen_spa_pvalue: float
    passes_statistical_gate: bool


def partition_frozen_holdout(
    timestamps: Sequence[datetime],
    *,
    holdout_days: int = 180,
    fallback_fraction: float = 0.20,
) -> FrozenHoldoutPartition:
    """Отделяет неприкосновенный финальный период (последние 6–12 месяцев, раздел 5.4 ТЗ)."""
    n = len(timestamps)
    if n < 5:
        return FrozenHoldoutPartition(
            dev_indices=tuple(range(max(0, n - 1))),
            holdout_indices=(n - 1,) if n > 0 else (),
            cutoff_timestamp=timestamps[-1] if timestamps else datetime.min,
            holdout_months=0.0,
        )

    total_span = timestamps[-1] - timestamps[0]
    target_delta = timedelta(days=holdout_days)
    if total_span > target_delta * 1.5:
        cutoff_ts = timestamps[-1] - target_delta
        dev_idx = [i for i, ts in enumerate(timestamps) if ts < cutoff_ts]
        hold_idx = [i for i, ts in enumerate(timestamps) if ts >= cutoff_ts]
    else:
        split_at = max(2, int(n * (1.0 - fallback_fraction)))
        dev_idx = list(range(split_at))
        hold_idx = list(range(split_at, n))
        cutoff_ts = timestamps[split_at]

    hold_span_days = (
        (timestamps[hold_idx[-1]] - timestamps[hold_idx[0]]).total_seconds() / 86400.0
        if len(hold_idx) >= 2
        else 0.0
    )
    return FrozenHoldoutPartition(
        dev_indices=tuple(dev_idx),
        holdout_indices=tuple(hold_idx),
        cutoff_timestamp=cutoff_ts,
        holdout_months=hold_span_days / 30.4375,
    )


def build_purged_kfold_splits(
    t_starts: Sequence[datetime],
    t_ends: Sequence[datetime],
    *,
    n_splits: int = 5,
    embargo_pct: float = 0.02,
) -> tuple[PurgedFoldSplit, ...]:
    """Строит Purged K-Fold + Embargo (López de Prado, 2018, глава 7).

    Для каждого тестового блока ``[test_start_ts, test_end_ts]``:
    - **Purging**: из обучения исключаются все наблюдения ``i``, чей интервал разметки
      ``[t_starts[i], t_ends[i]]`` пересекается с интервалом тестового фолда:
      ``t_starts[i] <= test_end_ts and t_ends[i] >= test_start_ts``.
    - **Embargo**: после конца тестового фолда дополнительно исключается буфер из
      ``ceil(N * embargo_pct)`` наблюдений.
    """
    n = len(t_starts)
    if n < n_splits or n != len(t_ends):
        msg = f"Недостаточно наблюдений ({n}) для {n_splits} фолдов Purged K-Fold"
        raise ValueError(msg)

    embargo_len = max(1, math.ceil(n * embargo_pct))
    fold_sizes = [n // n_splits + (1 if i < (n % n_splits) else 0) for i in range(n_splits)]

    groups: list[tuple[int, int]] = []
    cursor = 0
    for sz in fold_sizes:
        groups.append((cursor, cursor + sz))
        cursor += sz

    splits: list[PurgedFoldSplit] = []
    for fold_id, (test_lo, test_hi) in enumerate(groups):
        test_indices = tuple(range(test_lo, test_hi))
        test_start_ts = t_starts[test_lo]
        test_end_ts = max(t_ends[test_lo:test_hi])

        embargo_hi = min(n, test_hi + embargo_len)
        embargo_range = set(range(test_hi, embargo_hi))

        train_list: list[int] = []
        purged_cnt = 0
        embargo_cnt = 0

        for i in range(n):
            if test_lo <= i < test_hi:
                continue
            if i in embargo_range:
                embargo_cnt += 1
                continue
            # Проверка пересечения интервала метки [t_starts[i], t_ends[i]] с тестовым окном
            overlaps_test = t_starts[i] <= test_end_ts and t_ends[i] >= test_start_ts
            if overlaps_test:
                purged_cnt += 1
                continue
            train_list.append(i)

        splits.append(
            PurgedFoldSplit(
                fold_id=fold_id,
                train_indices=tuple(train_list),
                test_indices=test_indices,
                purged_count=purged_cnt,
                embargoed_count=embargo_cnt,
                test_group_ids=(fold_id,),
            )
        )
    return tuple(splits)


def build_cpcv_splits(
    t_starts: Sequence[datetime],
    t_ends: Sequence[datetime],
    *,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_pct: float = 0.02,
) -> tuple[PurgedFoldSplit, ...]:
    """Строит Combinatorial Purged Cross-Validation (CPCV, раздел 5.2 ТЗ).

    Делит историю на ``n_groups`` хронологических блоков и формирует все
    ``C(n_groups, k_test_groups)`` комбинаций тестовых групп с Purging и Embargo.
    """
    n = len(t_starts)
    if n < n_groups or k_test_groups <= 0 or k_test_groups >= n_groups:
        msg = f"Некорректные параметры CPCV: n={n}, n_groups={n_groups}, k={k_test_groups}"
        raise ValueError(msg)

    embargo_len = max(1, math.ceil(n * embargo_pct))
    group_sizes = [n // n_groups + (1 if g < (n % n_groups) else 0) for g in range(n_groups)]
    bounds: list[tuple[int, int]] = []
    cur = 0
    for sz in group_sizes:
        bounds.append((cur, cur + sz))
        cur += sz

    splits: list[PurgedFoldSplit] = []
    for fold_id, test_g_tuple in enumerate(itertools.combinations(range(n_groups), k_test_groups)):
        test_set: set[int] = set()
        embargo_set: set[int] = set()
        test_intervals: list[tuple[datetime, datetime]] = []

        for g_id in test_g_tuple:
            lo, hi = bounds[g_id]
            test_set.update(range(lo, hi))
            test_intervals.append((t_starts[lo], max(t_ends[lo:hi])))
            emb_hi = min(n, hi + embargo_len)
            embargo_set.update(range(hi, emb_hi))

        embargo_set -= test_set
        train_list: list[int] = []
        purged_cnt = 0
        embargo_cnt = len(embargo_set)

        for i in range(n):
            if i in test_set or i in embargo_set:
                continue
            overlaps = any(
                t_starts[i] <= int_end and t_ends[i] >= int_start
                for int_start, int_end in test_intervals
            )
            if overlaps:
                purged_cnt += 1
                continue
            train_list.append(i)

        splits.append(
            PurgedFoldSplit(
                fold_id=fold_id,
                train_indices=tuple(train_list),
                test_indices=tuple(sorted(test_set)),
                purged_count=purged_cnt,
                embargoed_count=embargo_cnt,
                test_group_ids=test_g_tuple,
            )
        )
    return tuple(splits)


def compute_annualized_sharpe(
    returns: Sequence[float],
    *,
    bars_per_year: float = 252.0 * 9.0,
) -> float:
    """Вычисляет аннуализированный коэффициент Шарпа ряда доходностей."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_r = sum(returns) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(var_r)
    if std_r <= 1e-12:
        return 0.0
    return (mean_r / std_r) * math.sqrt(bars_per_year)


def compute_deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    n_trials: int = 10,
    trials_sharpe_std: float = 0.35,
) -> tuple[float, float, float]:
    """Вычисляет Deflated Sharpe Ratio (DSR, Bailey & López de Prado, 2014).

    Возвращает кортеж ``(dsr_probability, non_annualized_sr, expected_max_sr0)``.
    Учитывает число проверенных конфигураций ``n_trials``, длину выборки ``T``,
    асимметрию (skewness) и эксцесс (kurtosis) доходностей.
    """
    n = len(returns)
    if n < 5:
        return 0.0, 0.0, 0.0

    mean_r = sum(returns) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(var_r)
    if std_r <= 1e-12:
        return 0.0, 0.0, 0.0

    sr_hat = mean_r / std_r
    skew = sum(((r - mean_r) / std_r) ** 3 for r in returns) / n
    kurt = sum(((r - mean_r) / std_r) ** 4 for r in returns) / n

    eff_trials = max(2, n_trials)
    # Ожидаемый максимум из eff_trials оценок Шарпа по распределению Гумбеля
    z1 = _norm_ppf(1.0 - 1.0 / eff_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (eff_trials * math.e))
    sr_0 = (trials_sharpe_std / math.sqrt(max(n, 2))) * (
        (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    )

    denom_sq = max(1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * (sr_hat * sr_hat), 1e-6)
    z_stat = ((sr_hat - sr_0) * math.sqrt(n - 1)) / math.sqrt(denom_sq)
    dsr_prob = _norm_cdf(z_stat)
    return dsr_prob, sr_hat, sr_0


def compute_pbo_cscv(
    strategy_returns_matrix: Sequence[Sequence[float]],
    *,
    n_partitions: int = 8,
) -> tuple[float, float]:
    """Вычисляет Probability of Backtest Overfitting (PBO) методом CSCV (Bailey et al., 2017).

    Вход ``strategy_returns_matrix`` имеет размер ``[n_strategies][n_timesteps]``.
    Делит временную ось на чётное число блоков ``n_partitions = 2B``, перебирает
    все ``C(2B, B)`` разбиений на In-Sample (IS) и Out-of-Sample (OOS), находит лучшую
    стратегию на IS и оценивает её относительный ранг на OOS.
    Возвращает ``(pbo_probability, median_logit)``.
    """
    n_strats = len(strategy_returns_matrix)
    if n_strats < 2:
        return 0.0, 0.0
    n_steps = len(strategy_returns_matrix[0])
    even_parts = max(4, (n_partitions // 2) * 2)
    if n_steps < even_parts * 2:
        return 0.25, 0.0

    part_size = n_steps // even_parts
    # Предвычисляем Шарп (или среднюю доходность) каждой стратегии в каждом блоке
    block_perf = [[0.0] * even_parts for _ in range(n_strats)]
    for s in range(n_strats):
        row = strategy_returns_matrix[s]
        for b in range(even_parts):
            sub = row[b * part_size : (b + 1) * part_size]
            block_perf[s][b] = compute_annualized_sharpe(sub)

    half = even_parts // 2
    all_blocks = set(range(even_parts))
    logits: list[float] = []

    for is_blocks in itertools.combinations(range(even_parts), half):
        oos_blocks = tuple(all_blocks - set(is_blocks))
        is_scores = [sum(block_perf[s][b] for b in is_blocks) for s in range(n_strats)]
        oos_scores = [sum(block_perf[s][b] for b in oos_blocks) for s in range(n_strats)]

        best_is_strat = max(range(n_strats), key=lambda s: is_scores[s])
        target_oos = oos_scores[best_is_strat]

        # Относительный ранг лучшей IS-стратегии на OOS в интервале (0, 1)
        rank = sum(1 for val in oos_scores if val <= target_oos)
        omega = min(max(rank / (n_strats + 1.0), 1e-4), 1.0 - 1e-4)
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        return 0.0, 0.0

    underperformed = sum(1 for lam in logits if lam <= 0.0)
    pbo = underperformed / len(logits)
    sorted_logits = sorted(logits)
    median_logit = sorted_logits[len(sorted_logits) // 2]
    return pbo, median_logit


def compute_white_rc_and_hansen_spa(
    strategy_returns_matrix: Sequence[Sequence[float]],
    benchmark_returns: Sequence[float],
    *,
    n_bootstrap: int = 200,
    block_length: int = 5,
    seed: int = 42,
) -> tuple[float, float]:
    """Вычисляет p-value тестов White's Reality Check (2000) и Hansen's SPA (2005).

    Проверяет нулевую гипотезу ``H0: max_k E[d_{k,t}] <= 0``, где
    ``d_{k,t} = r_{k,t} - r_{bench,t}`` — избыточная доходность ``k``-й стратегии
    над бенчмарком, с помощью стационарного блочного бутстрэпа Поли — Романо.
    Возвращает ``(white_rc_pvalue, hansen_spa_pvalue)``.
    """
    n_strats = len(strategy_returns_matrix)
    n_steps = len(benchmark_returns)
    if n_strats == 0 or n_steps < 5:
        return 1.0, 1.0

    # Матрица избыточных доходностей d[k][t]
    diffs: list[list[float]] = []
    mean_d: list[float] = []
    omega_k: list[float] = []

    for k in range(n_strats):
        row = strategy_returns_matrix[k]
        m_len = min(len(row), n_steps)
        d_k = [row[t] - benchmark_returns[t] for t in range(m_len)]
        mu_k = sum(d_k) / m_len
        var_k = sum((x - mu_k) ** 2 for x in d_k) / max(m_len - 1, 1)
        diffs.append(d_k)
        mean_d.append(mu_k)
        omega_k.append(max(math.sqrt(var_k), 1e-8))

    sqrt_n = math.sqrt(n_steps)
    # Наблюдаемые статистики: ненормированная (White RC) и стьюдентизированная (Hansen SPA)
    t_rc_obs = max(0.0, max(sqrt_n * mean_d[k] for k in range(n_strats)))
    t_spa_obs = max(0.0, max((sqrt_n * mean_d[k]) / omega_k[k] for k in range(n_strats)))

    # Порог рецентрирования Хансена: -sqrt(2 * ln(ln(n)))
    log_log_n = math.log(max(math.log(max(n_steps, 3)), 1.0001))
    hansen_threshold = -math.sqrt(2.0 * log_log_n)
    recenter_mu = [
        mean_d[k] if ((sqrt_n * mean_d[k]) / omega_k[k]) >= hansen_threshold else 0.0
        for k in range(n_strats)
    ]

    rc_exceed = 0
    spa_exceed = 0
    rng_state = (seed * 1_664_525 + 1_013_904_223) & 0xFFFFFFFF
    geom_p = 1.0 / max(block_length, 1)

    for _b in range(n_bootstrap):
        # Генерируем индексы стационарного блочного бутстрэпа длины n_steps
        boot_indices: list[int] = []
        cur_idx = 0
        for t in range(n_steps):
            rng_state = (1_664_525 * rng_state + 1_013_904_223) & 0xFFFFFFFF
            u = rng_state / 4_294_967_296.0
            if t == 0 or u < geom_p:
                rng_state = (1_664_525 * rng_state + 1_013_904_223) & 0xFFFFFFFF
                cur_idx = int((rng_state / 4_294_967_296.0) * n_steps) % n_steps
            else:
                cur_idx = (cur_idx + 1) % n_steps
            boot_indices.append(cur_idx)

        max_rc_b = 0.0
        max_spa_b = 0.0
        for k in range(n_strats):
            d_k = diffs[k]
            m_len = len(d_k)
            boot_mean = sum(d_k[idx % m_len] for idx in boot_indices) / n_steps
            stat_rc_k = sqrt_n * (boot_mean - mean_d[k])
            stat_spa_k = (sqrt_n * (boot_mean - recenter_mu[k])) / omega_k[k]
            if stat_rc_k > max_rc_b:
                max_rc_b = stat_rc_k
            if stat_spa_k > max_spa_b:
                max_spa_b = stat_spa_k

        if max_rc_b >= t_rc_obs:
            rc_exceed += 1
        if max_spa_b >= t_spa_obs:
            spa_exceed += 1

    p_rc = rc_exceed / n_bootstrap
    p_spa = spa_exceed / n_bootstrap
    return p_rc, p_spa
