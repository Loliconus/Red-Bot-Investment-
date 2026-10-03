"""Разметка целевой переменной «Синтетического трейдера» (раздел 2 ТЗ).

Реализует два взаимодополняющих метода:
1. **Triple-barrier method** (López de Prado, *Advances in Financial Machine Learning*, 2018):
   верхний барьер ``+k_tp * ATR_t``, нижний ``-k_sl * ATR_t``, вертикальный таймаут ``H`` баров.
   Естественным образом порождает 3 целевые переменные для вероятностных голов:
   - ``y_trend`` ∈ {0, 1} — для ``P(trend)`` (пробит ли направленный барьер против таймаута);
   - ``y_up_given_trend`` ∈ {0, 1, None} — для ``P(up | trend)``;
   - ``y_break_within_h`` ∈ {0, 1} — для ``P(break within H)`` (слом тренда на горизонте H).
2. **ℓ1-trend filtering** (Kim, Koh, Boyd, Gorinevsky, *SIAM Review*, 2009):
   выпуклая оптимизация ``min_x 0.5 * ||y - x||_2^2 + lambda * ||D^(2) x||_1`` через двойственный
   покоординатный спуск, аппроксимирующая лог-цену кусочно-линейным трендом и выделяющая:
   - наклон сегмента (знак и силу тренда),
   - точки излома (knots / смену режима),
   - остаточный шум ``y - x``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.domain.value_objects import OHLCV


@dataclass(frozen=True, slots=True, kw_only=True)
class TripleBarrierConfig:
    """Параметры тройного барьера, адаптированные к текущей волатильности ATR."""

    horizon_bars: int = 12
    k_tp: Decimal = Decimal("1.8")
    k_sl: Decimal = Decimal("1.2")
    atr_period: int = 14
    min_atr_pct: Decimal = Decimal("0.002")


@dataclass(frozen=True, slots=True, kw_only=True)
class TripleBarrierEvent:
    """Результат разметки одного бара методом тройного барьера.

    Атрибуты ``t_start`` и ``t_end`` используются в Purged K-Fold и CPCV
    для удаления перекрывающихся во времени меток между train и test.
    """

    index: int
    t_start: datetime
    t_end: datetime
    end_index: int
    entry_price: Decimal
    upper_barrier: Decimal
    lower_barrier: Decimal
    atr: Decimal
    barrier_label: int  # +1 (upper), -1 (lower), 0 (vertical timeout)
    realized_return: Decimal
    bars_to_touch: int
    y_trend: int  # 1 если направленное движение (+1/-1), 0 если боковик (0)
    y_up_given_trend: int | None  # 1 при +1, 0 при -1, None при 0
    y_break_within_h: int  # 1 если текущий тренд сломлен на горизонте H


@dataclass(frozen=True, slots=True, kw_only=True)
class L1TrendSegmentPoint:
    """Точка разложения ряда методом ℓ1-фильтрации тренда."""

    index: int
    timestamp: datetime
    log_price: float
    trend_level: float
    slope: float
    second_diff: float
    residual_noise: float
    is_knot: bool
    regime_sign: int  # +1 восходящий сегмент, -1 нисходящий, 0 плоский


@dataclass(frozen=True, slots=True, kw_only=True)
class L1TrendFilterResult:
    """Полный результат ℓ1-trend filtering на историческом окне."""

    lambda_reg: float
    points: tuple[L1TrendSegmentPoint, ...]
    knot_indices: tuple[int, ...]
    residual_std: float
    dual_gap: float


def compute_wilder_atr_series(
    candles: Sequence[OHLCV],
    period: int = 14,
) -> list[Decimal]:
    """Вычисляет каузальную серию ATR Уайлдера для каждого бара (без заглядывания вперёд)."""
    n = len(candles)
    if n == 0:
        return []
    tr_list: list[Decimal] = []
    for idx, bar in enumerate(candles):
        if idx == 0:
            tr_list.append(bar.high - bar.low)
        else:
            prev_close = candles[idx - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            tr_list.append(tr)

    atr_series: list[Decimal] = []
    period_dec = Decimal(period)
    running = Decimal("0")
    for idx, tr in enumerate(tr_list):
        if idx < period:
            running += tr
            atr_series.append(running / Decimal(idx + 1))
        else:
            prev_atr = atr_series[-1]
            cur_atr = (prev_atr * Decimal(period - 1) + tr) / period_dec
            atr_series.append(cur_atr)
    return atr_series


def solve_l1_trend_filter(
    candles: Sequence[OHLCV],
    *,
    lambda_reg: float = 2.5,
    max_iterations: int = 400,
    tol: float = 1e-6,
    knot_threshold: float = 1e-3,
    slope_deadband: float = 4e-4,
) -> L1TrendFilterResult:
    """Решает задачу ℓ1-фильтрации тренда (Kim, Koh, Boyd, Gorinevsky, 2009).

    Прямая задача::

        min_x  0.5 * ||y - x||_2^2 + lambda * ||D^(2) x||_1

    Двойственная задача (проекционный покоординатный спуск с ускорением)::

        min_v  0.5 * ||y - (D^(2))^T v||_2^2   при  ||v||_inf <= lambda,
        где x* = y - (D^(2))^T v*.

    Строка ``i`` матрицы ``D^(2)`` имеет коэффициенты ``(1, -2, 1)`` и норму ``||d_i||_2^2 = 6``.
    Один проход по всем координатам выполняется за ``O(N)``.
    """
    n = len(candles)
    if n == 0:
        return L1TrendFilterResult(
            lambda_reg=lambda_reg,
            points=(),
            knot_indices=(),
            residual_std=0.0,
            dual_gap=0.0,
        )

    y = [math.log(max(float(c.close), 1e-9)) for c in candles]
    if n < 3 or lambda_reg <= 0.0:
        pts = tuple(
            L1TrendSegmentPoint(
                index=i,
                timestamp=candles[i].timestamp,
                log_price=y[i],
                trend_level=y[i],
                slope=(y[i] - y[i - 1]) if i > 0 else 0.0,
                second_diff=0.0,
                residual_noise=0.0,
                is_knot=False,
                regime_sign=0,
            )
            for i in range(n)
        )
        return L1TrendFilterResult(
            lambda_reg=lambda_reg,
            points=pts,
            knot_indices=(),
            residual_std=0.0,
            dual_gap=0.0,
        )

    m = n - 2
    nu = [0.0] * m
    # Поддерживаем текущую оценку тренда x = y - D^T * nu
    x = list(y)

    for _iteration in range(max_iterations):
        max_step = 0.0
        for i in range(m):
            # (D x)_i = x[i] - 2*x[i+1] + x[i+2]
            dx_i = x[i] - 2.0 * x[i + 1] + x[i + 2]
            old_nu = nu[i]
            candidate = old_nu + dx_i / 6.0
            if candidate > lambda_reg:
                new_nu = lambda_reg
            elif candidate < -lambda_reg:
                new_nu = -lambda_reg
            else:
                new_nu = candidate

            delta = new_nu - old_nu
            if delta != 0.0:
                nu[i] = new_nu
                x[i] -= delta
                x[i + 1] += 2.0 * delta
                x[i + 2] -= delta
                abs_delta = abs(delta)
                if abs_delta > max_step:
                    max_step = abs_delta
        if max_step < tol:
            break

    # Вычисляем разности, изломы и остаточный шум
    slopes: list[float] = [0.0] * n
    for i in range(1, n):
        slopes[i] = x[i] - x[i - 1]
    if n > 1:
        slopes[0] = slopes[1]

    second_diffs: list[float] = [0.0] * n
    knots: list[int] = []
    for i in range(1, n - 1):
        d2 = x[i - 1] - 2.0 * x[i] + x[i + 1]
        second_diffs[i] = d2
        prev_sign = 1 if slopes[i] > slope_deadband else (-1 if slopes[i] < -slope_deadband else 0)
        next_sign = (
            1
            if slopes[i + 1] > slope_deadband
            else (-1 if slopes[i + 1] < -slope_deadband else 0)
        )
        sign_flip = prev_sign != 0 and next_sign != 0 and prev_sign != next_sign
        if abs(d2) >= knot_threshold or sign_flip:
            knots.append(i)

    residuals = [y[i] - x[i] for i in range(n)]
    mean_res = sum(residuals) / n
    var_res = sum((r - mean_res) ** 2 for r in residuals) / max(n - 1, 1)
    residual_std = math.sqrt(var_res)

    # Оценка разрыва двойственности (duality gap)
    primal_l1 = sum(abs(x[i] - 2.0 * x[i + 1] + x[i + 2]) for i in range(m))
    dual_dot = sum(nu[i] * (x[i] - 2.0 * x[i + 1] + x[i + 2]) for i in range(m))
    dual_gap = abs(lambda_reg * primal_l1 - dual_dot)

    points: list[L1TrendSegmentPoint] = []
    knot_set = set(knots)
    for i in range(n):
        s = slopes[i]
        reg_sign = 1 if s > slope_deadband else (-1 if s < -slope_deadband else 0)
        points.append(
            L1TrendSegmentPoint(
                index=i,
                timestamp=candles[i].timestamp,
                log_price=y[i],
                trend_level=x[i],
                slope=s,
                second_diff=second_diffs[i],
                residual_noise=residuals[i],
                is_knot=i in knot_set,
                regime_sign=reg_sign,
            )
        )

    return L1TrendFilterResult(
        lambda_reg=lambda_reg,
        points=tuple(points),
        knot_indices=tuple(knots),
        residual_std=residual_std,
        dual_gap=dual_gap,
    )


def build_triple_barrier_events(
    candles: Sequence[OHLCV],
    config: TripleBarrierConfig | None = None,
    *,
    l1_lambda: float = 2.0,
) -> tuple[TripleBarrierEvent, ...]:
    """Размечает историческую серию свечей методом Triple-Barrier + ℓ1-изломами.

    Вокруг каждой точки ``t`` ставятся три барьера:
    - верхний (take-profit): ``entry * (1 + k_tp * atr_pct)`` (или ``entry + k_tp * atr``),
    - нижний (stop-loss): ``entry - k_sl * atr``,
    - вертикальный (timeout): ``t + H`` баров.

    Возвращает кортеж ``TripleBarrierEvent`` для всех баров, имеющих полный
    горизонт ``H`` (или коснувшихся барьера раньше конца выборки).
    """
    cfg = config or TripleBarrierConfig()
    n = len(candles)
    if n < 2:
        return ()

    atr_series = compute_wilder_atr_series(candles, period=cfg.atr_period)
    l1_result = solve_l1_trend_filter(candles, lambda_reg=l1_lambda)
    knot_set = set(l1_result.knot_indices)

    events: list[TripleBarrierEvent] = []
    horizon = max(cfg.horizon_bars, 1)

    for idx in range(n - 1):
        entry_bar = candles[idx]
        entry_price = entry_bar.close
        if entry_price <= Decimal("0"):
            continue

        raw_atr = atr_series[idx]
        min_atr = entry_price * cfg.min_atr_pct
        effective_atr = max(raw_atr, min_atr)

        upper = entry_price + cfg.k_tp * effective_atr
        lower = max(Decimal("0.0001"), entry_price - cfg.k_sl * effective_atr)

        max_j = min(n - 1, idx + horizon)
        barrier_label = 0
        touch_idx = max_j

        # Определяем текущее локальное направление до точки idx (каузально)
        lookback_idx = max(0, idx - min(horizon, 6))
        prev_diff = entry_price - candles[lookback_idx].close
        current_dir = 1 if prev_diff > Decimal("0") else (-1 if prev_diff < Decimal("0") else 0)
        if current_dir == 0 and idx < len(l1_result.points):
            current_dir = l1_result.points[idx].regime_sign

        reversal_touched = False
        knot_in_horizon = False

        for j in range(idx + 1, max_j + 1):
            future_bar = candles[j]
            if j in knot_set:
                knot_in_horizon = True

            hit_upper = future_bar.high >= upper
            hit_lower = future_bar.low <= lower

            if current_dir > 0 and hit_lower:
                reversal_touched = True
            elif current_dir < 0 and hit_upper:
                reversal_touched = True

            if barrier_label == 0:
                if hit_upper and hit_lower:
                    # Если оба барьера внутри одной свечи — смотрим направление закрытия
                    if future_bar.close >= entry_price:
                        barrier_label = 1
                    else:
                        barrier_label = -1
                    touch_idx = j
                elif hit_upper:
                    barrier_label = 1
                    touch_idx = j
                elif hit_lower:
                    barrier_label = -1
                    touch_idx = j

        # Если горизонта недостаточно и барьер не пробит — пропускаем хвостовой неполный бар
        if barrier_label == 0 and idx + horizon >= n:
            continue

        end_bar = candles[touch_idx]
        realized_ret = (end_bar.close - entry_price) / entry_price
        y_trend = 1 if barrier_label != 0 else 0
        y_up: int | None = 1 if barrier_label == 1 else (0 if barrier_label == -1 else None)

        # Слом тренда на горизонте H: либо излом ℓ1-тренда, либо удар в противоположный барьер
        opposite_barrier = (current_dir > 0 and barrier_label == -1) or (
            current_dir < 0 and barrier_label == 1
        )
        y_break = 1 if (reversal_touched or opposite_barrier or knot_in_horizon) else 0

        events.append(
            TripleBarrierEvent(
                index=idx,
                t_start=entry_bar.timestamp,
                t_end=end_bar.timestamp,
                end_index=touch_idx,
                entry_price=entry_price,
                upper_barrier=upper,
                lower_barrier=lower,
                atr=effective_atr,
                barrier_label=barrier_label,
                realized_return=realized_ret,
                bars_to_touch=touch_idx - idx,
                y_trend=y_trend,
                y_up_given_trend=y_up,
                y_break_within_h=y_break,
            )
        )

    return tuple(events)
