"""Этап 0: Марковское переключение режимов (Hamilton, 1989) / 3-состояния HMM (раздел 4 ТЗ).

Скрытые состояния рынка:
- ``TREND`` — направленный режим (выраженный дрейф по модулю, умеренная волатильность);
- ``CHOP``  — «пила» / боковик (околонулевой дрейф, возврат к среднему);
- ``PANIC`` — паника / экстремальная волатильность (запрет открытия позиций в риск-слое).

Алгоритмы:
- Каузальный фильтр Гамильтона ``P(S_t = k | F_t)`` (без заглядывания в будущее — для живой
  торговли и признаков этапов 1–2 CatBoost).
- Алгоритм Баума — Уэлша (EM) с каноническим упорядочиванием состояний.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from core.domain.value_objects import OHLCV


class SyntheticMarketRegime(StrEnum):
    """Три базовых скрытых состояния рынка по модели Гамильтона."""

    TREND = "trend"
    CHOP = "chop"
    PANIC = "panic"


@dataclass(frozen=True, slots=True, kw_only=True)
class GaussianEmission2D:
    """Параметры диагонального двумерного гауссиана (дрейф ``|r_k|``, волатильность ``sigma_k``)."""

    mean_drift: float
    std_drift: float
    mean_vol: float
    std_vol: float

    def log_pdf(self, drift_obs: float, vol_obs: float) -> float:
        sd = max(self.std_drift, 1e-5)
        sv = max(self.std_vol, 1e-5)
        z_d = (drift_obs - self.mean_drift) / sd
        z_v = (vol_obs - self.mean_vol) / sv
        return -0.5 * (z_d * z_d + z_v * z_v) - math.log(2.0 * math.pi * sd * sv)


@dataclass(frozen=True, slots=True, kw_only=True)
class HMMRegimePosterior:
    """Апостериорное распределение скрытых состояний на момент закрытия бара ``t``."""

    prob_trend: float
    prob_chop: float
    prob_panic: float
    dominant_regime: SyntheticMarketRegime
    expected_duration_bars: float
    is_trading_banned: bool

    def as_feature_dict(self) -> dict[str, float]:
        return {
            "hmm_prob_trend": self.prob_trend,
            "hmm_prob_chop": self.prob_chop,
            "hmm_prob_panic": self.prob_panic,
            "hmm_expected_duration": self.expected_duration_bars,
        }


def _extract_observations(
    candles: Sequence[OHLCV],
    *,
    drift_window: int = 5,
    vol_window: int = 10,
) -> list[tuple[float, float]]:
    """Преобразует свечи в пары наблюдений ``(directional_drift, realized_vol)``."""
    n = len(candles)
    if n == 0:
        return []
    closes = [max(float(c.close), 1e-9) for c in candles]
    log_rets = [0.0]
    for i in range(1, n):
        log_rets.append(math.log(closes[i] / closes[i - 1]))

    obs: list[tuple[float, float]] = []
    for i in range(n):
        d_start = max(0, i - drift_window + 1)
        d_slice = log_rets[d_start : i + 1]
        # Направленный модуль средней доходности на окне drift_window
        abs_drift = abs(sum(d_slice)) / math.sqrt(len(d_slice))

        v_start = max(0, i - vol_window + 1)
        v_slice = log_rets[v_start : i + 1]
        if len(v_slice) >= 2:
            mean_v = sum(v_slice) / len(v_slice)
            var_v = sum((r - mean_v) ** 2 for r in v_slice) / (len(v_slice) - 1)
            r_vol = math.sqrt(var_v)
        else:
            r_vol = abs(log_rets[i])
        obs.append((abs_drift, r_vol))
    return obs


def _logsumexp(log_vals: Sequence[float]) -> float:
    m = max(log_vals)
    if not math.isfinite(m):
        return -1e9
    return m + math.log(sum(math.exp(v - m) for v in log_vals))


class HamiltonMarkovRegimeDetector:
    """3-состояния HMM / Markov-Switching фильтр Гамильтона (1989).

    Индексы состояний фиксированы семантически:
    - ``0``: ``TREND``
    - ``1``: ``CHOP``
    - ``2``: ``PANIC``
    """

    def __init__(
        self,
        *,
        panic_ban_threshold: float = 0.45,
    ) -> None:
        self.panic_ban_threshold = panic_ban_threshold
        self.initial_probs: list[float] = [0.40, 0.50, 0.10]
        # Матрица переходов A[i][j] = P(S_t = j | S_{t-1} = i)
        self.transition_matrix: list[list[float]] = [
            [0.86, 0.11, 0.03],
            [0.14, 0.83, 0.03],
            [0.10, 0.15, 0.75],
        ]
        self.emissions: list[GaussianEmission2D] = [
            GaussianEmission2D(
                mean_drift=0.012,
                std_drift=0.008,
                mean_vol=0.009,
                std_vol=0.005,
            ),
            GaussianEmission2D(
                mean_drift=0.002,
                std_drift=0.003,
                mean_vol=0.005,
                std_vol=0.003,
            ),
            GaussianEmission2D(
                mean_drift=0.025,
                std_drift=0.020,
                mean_vol=0.028,
                std_vol=0.015,
            ),
        ]

    def fit(
        self,
        candles: Sequence[OHLCV],
        *,
        max_em_iterations: int = 20,
        tol: float = 1e-4,
    ) -> HamiltonMarkovRegimeDetector:
        """Оценивает параметры эмиссий и матрицы переходов алгоритмом Баума — Уэлша (EM)."""
        obs = _extract_observations(candles)
        n = len(obs)
        if n < 15:
            return self

        # Инициализация масштабов по квантилям выборки
        drifts = sorted(d for d, _ in obs)
        vols = sorted(v for _, v in obs)
        d_med = max(drifts[n // 2], 1e-4)
        d_p80 = max(drifts[min(n - 1, int(n * 0.80))], d_med * 1.5)
        v_med = max(vols[n // 2], 1e-4)
        v_p90 = max(vols[min(n - 1, int(n * 0.90))], v_med * 2.0)
        panic_vol_floor = max(v_p90 * 1.35, v_med * 2.20, 0.0075)

        self.emissions = [
            # 0: TREND — высокий направленный дрейф, умеренная волатильность
            GaussianEmission2D(
                mean_drift=d_p80,
                std_drift=max(d_med * 0.7, 1e-4),
                mean_vol=v_med * 1.05,
                std_vol=max(v_med * 0.45, 1e-4),
            ),
            # 1: CHOP — малый дрейф, низкая/средняя волатильность
            GaussianEmission2D(
                mean_drift=d_med * 0.35,
                std_drift=max(d_med * 0.4, 1e-4),
                mean_vol=v_med * 0.75,
                std_vol=max(v_med * 0.4, 1e-4),
            ),
            # 2: PANIC — экстремальный всплеск волатильности (>= 2.2x медианы)
            GaussianEmission2D(
                mean_drift=d_p80 * 1.3,
                std_drift=max(d_p80, 1e-4),
                mean_vol=panic_vol_floor,
                std_vol=max(panic_vol_floor * 0.5, 1e-4),
            ),
        ]

        k_states = 3
        prev_ll = -1e18

        for _em_iter in range(max_em_iterations):
            log_b = [
                [self.emissions[k].log_pdf(d_t, v_t) for k in range(k_states)]
                for d_t, v_t in obs
            ]
            log_a = [
                [math.log(max(self.transition_matrix[i][j], 1e-9)) for j in range(k_states)]
                for i in range(k_states)
            ]

            # Forward pass (в лог-шкале)
            log_alpha = [[0.0] * k_states for _ in range(n)]
            for k in range(k_states):
                log_alpha[0][k] = math.log(max(self.initial_probs[k], 1e-9)) + log_b[0][k]

            for t in range(1, n):
                for j in range(k_states):
                    log_alpha[t][j] = log_b[t][j] + _logsumexp(
                        [log_alpha[t - 1][i] + log_a[i][j] for i in range(k_states)]
                    )

            total_ll = _logsumexp(log_alpha[-1])

            # Backward pass
            log_beta = [[0.0] * k_states for _ in range(n)]
            for t in range(n - 2, -1, -1):
                for i in range(k_states):
                    log_beta[t][i] = _logsumexp(
                        [
                            log_a[i][j] + log_b[t + 1][j] + log_beta[t + 1][j]
                            for j in range(k_states)
                        ]
                    )

            # Апостериорные вероятности состояний gamma[t][k]
            gamma = [[0.0] * k_states for _ in range(n)]
            for t in range(n):
                denom = _logsumexp([log_alpha[t][k] + log_beta[t][k] for k in range(k_states)])
                for k in range(k_states):
                    gamma[t][k] = math.exp(log_alpha[t][k] + log_beta[t][k] - denom)

            # Обновление матрицы переходов с регуляризацией Дирихле (sticky HMM)
            for i in range(k_states):
                row_xi = [0.15 if i != j else 1.5 for j in range(k_states)]
                for t in range(n - 1):
                    denom_t = _logsumexp(
                        [log_alpha[t][k] + log_beta[t][k] for k in range(k_states)]
                    )
                    for j in range(k_states):
                        log_xi_ij = (
                            log_alpha[t][i]
                            + log_a[i][j]
                            + log_b[t + 1][j]
                            + log_beta[t + 1][j]
                            - denom_t
                        )
                        row_xi[j] += math.exp(max(min(log_xi_ij, 0.0), -50.0))
                row_sum = sum(row_xi)
                self.transition_matrix[i] = [val / row_sum for val in row_xi]

            # Обновление параметров гауссианов с сохранением семантического порядка состояний
            new_emissions: list[GaussianEmission2D] = []
            for k in range(k_states):
                w_sum = sum(gamma[t][k] for t in range(n)) + 1e-6
                m_d = sum(gamma[t][k] * obs[t][0] for t in range(n)) / w_sum
                m_v = sum(gamma[t][k] * obs[t][1] for t in range(n)) / w_sum
                s_d = math.sqrt(
                    sum(gamma[t][k] * (obs[t][0] - m_d) ** 2 for t in range(n)) / w_sum
                    + 1e-7
                )
                s_v = math.sqrt(
                    sum(gamma[t][k] * (obs[t][1] - m_v) ** 2 for t in range(n)) / w_sum
                    + 1e-7
                )
                new_emissions.append(
                    GaussianEmission2D(
                        mean_drift=m_d,
                        std_drift=s_d,
                        mean_vol=m_v,
                        std_vol=s_v,
                    )
                )

            # Гарантируем канонический порядок:
            # PANIC (индекс 2) — максимальная волатильность;
            # из оставшихся двух TREND (индекс 0) — больший модуль дрейфа, CHOP (1) — меньший.
            panic_idx = max(range(k_states), key=lambda idx: new_emissions[idx].mean_vol)
            rem = [idx for idx in range(k_states) if idx != panic_idx]
            if new_emissions[rem[0]].mean_drift >= new_emissions[rem[1]].mean_drift:
                trend_idx, chop_idx = rem[0], rem[1]
            else:
                trend_idx, chop_idx = rem[1], rem[0]

            order = [trend_idx, chop_idx, panic_idx]
            ordered_em = [new_emissions[idx] for idx in order]
            # Запрещаем кластеру PANIC схлопываться на обычную трендовую волатильность
            trend_em = ordered_em[0]
            panic_em = ordered_em[2]
            min_panic_v = max(panic_vol_floor, trend_em.mean_vol * 1.85)
            if panic_em.mean_vol < min_panic_v:
                ordered_em[2] = GaussianEmission2D(
                    mean_drift=max(panic_em.mean_drift, d_p80 * 1.15),
                    std_drift=max(panic_em.std_drift, d_med * 0.8),
                    mean_vol=min_panic_v,
                    std_vol=max(panic_em.std_vol, min_panic_v * 0.45),
                )
            self.emissions = ordered_em
            self.transition_matrix = [
                [self.transition_matrix[r][c] for c in order] for r in order
            ]

            if abs(total_ll - prev_ll) < tol:
                break
            prev_ll = total_ll

        return self

    def filter_causal_series(
        self,
        candles: Sequence[OHLCV],
    ) -> tuple[HMMRegimePosterior, ...]:
        """Каузальный фильтр Гамильтона ``P(S_t | y_{1:t})`` без заглядывания в будущее."""
        obs = _extract_observations(candles)
        if not obs:
            return ()

        k_states = 3
        state_probs = list(self.initial_probs)
        posteriors: list[HMMRegimePosterior] = []

        for t, (d_t, v_t) in enumerate(obs):
            if t > 0:
                # Шаг предсказания: P(S_t = j | y_{1:t-1}) = sum_i P(S_{t-1}=i) * A[i][j]
                predicted = [0.0] * k_states
                for j in range(k_states):
                    predicted[j] = sum(
                        state_probs[i] * self.transition_matrix[i][j] for i in range(k_states)
                    )
            else:
                predicted = list(state_probs)

            # Шаг обновления Байеса по наблюдению (d_t, v_t)
            log_liks = [self.emissions[k].log_pdf(d_t, v_t) for k in range(k_states)]
            max_ll = max(log_liks)
            unnorm = [
                max(predicted[k], 1e-9) * math.exp(max(log_liks[k] - max_ll, -50.0))
                for k in range(k_states)
            ]
            norm_sum = sum(unnorm)
            state_probs = [u / norm_sum for u in unnorm] if norm_sum > 0 else [0.4, 0.5, 0.1]

            p_trend, p_chop, p_panic = state_probs[0], state_probs[1], state_probs[2]
            if p_panic >= self.panic_ban_threshold or (p_panic >= p_trend and p_panic >= p_chop):
                dom = SyntheticMarketRegime.PANIC
                dom_idx = 2
            elif p_trend >= p_chop:
                dom = SyntheticMarketRegime.TREND
                dom_idx = 0
            else:
                dom = SyntheticMarketRegime.CHOP
                dom_idx = 1

            stay_prob = min(max(self.transition_matrix[dom_idx][dom_idx], 0.05), 0.99)
            expected_dur = 1.0 / (1.0 - stay_prob)
            banned = dom is SyntheticMarketRegime.PANIC or p_panic >= self.panic_ban_threshold

            posteriors.append(
                HMMRegimePosterior(
                    prob_trend=p_trend,
                    prob_chop=p_chop,
                    prob_panic=p_panic,
                    dominant_regime=dom,
                    expected_duration_bars=expected_dur,
                    is_trading_banned=banned,
                )
            )

        return tuple(posteriors)

    def detect_latest(self, candles: Sequence[OHLCV]) -> HMMRegimePosterior:
        """Возвращает апостериорное распределение скрытых состояний на последнем закрытом баре."""
        series = self.filter_causal_series(candles)
        if not series:
            return HMMRegimePosterior(
                prob_trend=0.0,
                prob_chop=1.0,
                prob_panic=0.0,
                dominant_regime=SyntheticMarketRegime.CHOP,
                expected_duration_bars=1.0,
                is_trading_banned=False,
            )
        return series[-1]
