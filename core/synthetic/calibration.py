"""Калибровка вероятностных голов «Синтетического трейдера» (раздел 4.1 ТЗ).

Градиентный бустинг оптимизирует логарифмический лосс и часто выдаёт смещённые к 0 и 1
оценки. Поскольку размер позиции напрямую зависит от ``P(trend)``, ``P(up|trend)`` и
``P(break within H)``, сырые выходы проходят обязательную пост-калибровку на отложенном
временном фолде:
- **Temperature Scaling** (масштабирование логитов температурой ``T > 0``);
- **Isotonic Regression** (алгоритм Pool Adjacent Violators — PAVA);
- Контроль качества через **Brier Score**, **ECE** и **Reliability Diagram** (диаграмму надёжности).
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from dataclasses import dataclass


def _clip_prob(p: float, eps: float = 1e-6) -> float:
    return min(max(p, eps), 1.0 - eps)


def _logit(p: float) -> float:
    pc = _clip_prob(p)
    return math.log(pc / (1.0 - pc))


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        ez = math.exp(-min(z, 60.0))
        return 1.0 / (1.0 + ez)
    ez = math.exp(max(z, -60.0))
    return ez / (1.0 + ez)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReliabilityBin:
    """Один бин диаграммы надёжности (Reliability Diagram)."""

    bin_index: int
    lower_bound: float
    upper_bound: float
    mean_predicted: float
    empirical_frequency: float
    sample_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationReport:
    """Метрики калибровки вероятностной головы до и после калибровки."""

    head_name: str
    method: str
    temperature: float
    brier_score_raw: float
    brier_score_calibrated: float
    brier_skill_score: float
    ece_raw: float
    ece_calibrated: float
    reliability_bins: tuple[ReliabilityBin, ...]


def compute_brier_score(probabilities: Sequence[float], targets: Sequence[int]) -> float:
    """Вычисляет Brier Score ``mean((p_i - y_i)^2)``."""
    n = len(probabilities)
    if n == 0 or n != len(targets):
        return 0.25
    return sum((_clip_prob(probabilities[i]) - float(targets[i])) ** 2 for i in range(n)) / n


def build_reliability_diagram(
    probabilities: Sequence[float],
    targets: Sequence[int],
    *,
    n_bins: int = 10,
) -> tuple[tuple[ReliabilityBin, ...], float]:
    """Строит диаграмму надёжности (Reliability Diagram) и считает ECE."""
    n = len(probabilities)
    bins: list[ReliabilityBin] = []
    ece = 0.0

    for b in range(n_bins):
        lo = b / n_bins
        hi = (b + 1) / n_bins
        idxs = [
            i
            for i in range(n)
            if (lo <= probabilities[i] < hi) or (b == n_bins - 1 and probabilities[i] == hi)
        ]
        count = len(idxs)
        if count > 0:
            mean_pred = sum(probabilities[i] for i in idxs) / count
            emp_freq = sum(float(targets[i]) for i in idxs) / count
            ece += (count / max(n, 1)) * abs(mean_pred - emp_freq)
        else:
            mean_pred = 0.5 * (lo + hi)
            emp_freq = 0.0

        bins.append(
            ReliabilityBin(
                bin_index=b,
                lower_bound=lo,
                upper_bound=hi,
                mean_predicted=mean_pred,
                empirical_frequency=emp_freq,
                sample_count=count,
            )
        )
    return tuple(bins), ece


class ProbabilityCalibrator:
    """Пост-калибратор вероятностей (Temperature Scaling + Isotonic PAVA)."""

    def __init__(self, *, method: str = "temperature") -> None:
        if method not in {"temperature", "isotonic", "hybrid"}:
            msg = f"Неизвестный метод калибровки: {method}"
            raise ValueError(msg)
        self.method = method
        self.temperature: float = 1.0
        self._iso_knots_x: list[float] = []
        self._iso_knots_y: list[float] = []

    def fit(
        self,
        raw_probabilities: Sequence[float],
        targets: Sequence[int],
    ) -> ProbabilityCalibrator:
        """Настраивает калибратор на отложенном валидационном временном фолде."""
        n = len(raw_probabilities)
        if n < 5 or n != len(targets):
            self.temperature = 1.0
            return self

        logits = [_logit(p) for p in raw_probabilities]
        y_float = [float(t) for t in targets]

        # 1. Подбор оптимальной температуры T (scipy.optimize.minimize_scalar при наличии SciPy)
        def _nll_for_temp(cand_t: float) -> float:
            t_val = max(float(cand_t), 0.25)
            loss = 0.0
            for i in range(n):
                p_cal = _clip_prob(_sigmoid(logits[i] / t_val))
                loss -= y_float[i] * math.log(p_cal) + (1.0 - y_float[i]) * math.log(1.0 - p_cal)
            return loss / n

        best_t = 1.0
        best_nll = _nll_for_temp(1.0)
        try:
            scipy_opt = importlib.import_module("scipy.optimize")
            minimize_scalar = getattr(scipy_opt, "minimize_scalar", None)
            if callable(minimize_scalar):
                opt_res = minimize_scalar(_nll_for_temp, bounds=(0.30, 2.40), method="bounded")
                if bool(getattr(opt_res, "success", False)) and float(opt_res.fun) <= best_nll:
                    best_t = float(opt_res.x)
                    best_nll = float(opt_res.fun)
        except ImportError:
            for step in range(30, 245, 5):
                cand_t = step / 100.0
                nll = _nll_for_temp(cand_t)
                if nll < best_nll:
                    best_nll = nll
                    best_t = cand_t
        self.temperature = min(max(best_t, 0.30), 2.40)

        # 2. Изотоническая регрессия (sklearn.isotonic.IsotonicRegression или алгоритм PAVA)
        self._iso_knots_x = []
        self._iso_knots_y = []
        pos_cnt = sum(1 for y in targets if y == 1)
        neg_cnt = n - pos_cnt
        if self.method in {"isotonic", "hybrid"} and pos_cnt >= 3 and neg_cnt >= 3:
            temp_scaled = [_sigmoid(logits[i] / self.temperature) for i in range(n)]
            used_sklearn = False
            try:
                sk_iso = importlib.import_module("sklearn.isotonic")
                iso_cls = getattr(sk_iso, "IsotonicRegression", None)
                if callable(iso_cls):
                    iso = iso_cls(y_min=0.02, y_max=0.98, out_of_bounds="clip")
                    iso.fit(temp_scaled, y_float)
                    thresholds_x = getattr(iso, "X_thresholds_", None)
                    thresholds_y = getattr(iso, "y_thresholds_", None)
                    if (
                        thresholds_x is not None
                        and thresholds_y is not None
                        and len(thresholds_x) >= 2
                    ):
                        self._iso_knots_x = [float(x) for x in thresholds_x]
                        self._iso_knots_y = [_clip_prob(float(y), eps=0.02) for y in thresholds_y]
                        used_sklearn = True
            except ImportError:
                used_sklearn = False

            if not used_sklearn:
                paired = sorted(zip(temp_scaled, y_float, strict=True), key=lambda pair: pair[0])
                blocks: list[list[float]] = []
                for x_val, y_val in paired:
                    blocks.append([y_val, 1.0, x_val, x_val])
                    while len(blocks) >= 2:
                        prev = blocks[-2]
                        curr = blocks[-1]
                        if prev[0] / prev[1] > curr[0] / curr[1]:
                            merged = [
                                prev[0] + curr[0],
                                prev[1] + curr[1],
                                prev[2],
                                curr[3],
                            ]
                            blocks.pop()
                            blocks[-1] = merged
                        else:
                            break
                self._iso_knots_x = [0.5 * (b[2] + b[3]) for b in blocks]
                self._iso_knots_y = [_clip_prob(b[0] / b[1], eps=0.02) for b in blocks]

            # Если на малой выборке изотоническая ступенька схлопнула диапазон (< 0.30),
            # добавляем крайние якоря [0.02, 0.98] для сохранения монотонности и чувствительности
            if (
                len(self._iso_knots_y) < 2
                or (max(self._iso_knots_y) - min(self._iso_knots_y)) < 0.30
            ):
                self._iso_knots_x = [0.02, *self._iso_knots_x, 0.98]
                self._iso_knots_y = [0.04, *self._iso_knots_y, 0.96]

        return self

    def calibrate_one(self, raw_p: float) -> float:
        """Калибрует одиночное вероятностное предсказание."""
        temp_p = _sigmoid(_logit(raw_p) / max(self.temperature, 0.05))
        if self.method == "temperature" or not self._iso_knots_x:
            return _clip_prob(temp_p)

        # Линейная интерполяция по узлам PAVA
        xs = self._iso_knots_x
        ys = self._iso_knots_y
        if temp_p <= xs[0]:
            iso_p = ys[0]
        elif temp_p >= xs[-1]:
            iso_p = ys[-1]
        else:
            iso_p = ys[-1]
            for k in range(len(xs) - 1):
                if xs[k] <= temp_p <= xs[k + 1]:
                    span = xs[k + 1] - xs[k]
                    w = (temp_p - xs[k]) / span if span > 1e-9 else 0.5
                    iso_p = ys[k] * (1.0 - w) + ys[k + 1] * w
                    break

        if self.method == "hybrid":
            return _clip_prob(0.5 * temp_p + 0.5 * iso_p)
        return _clip_prob(iso_p)

    def calibrate_batch(self, raw_probabilities: Sequence[float]) -> tuple[float, ...]:
        """Калибрует пакет вероятностей."""
        return tuple(self.calibrate_one(p) for p in raw_probabilities)

    def evaluate(
        self,
        head_name: str,
        raw_probabilities: Sequence[float],
        targets: Sequence[int],
    ) -> CalibrationReport:
        """Строит полный отчёт калибровки (Brier Score, Brier Skill Score, ECE, Reliability)."""
        calibrated = self.calibrate_batch(raw_probabilities)
        bs_raw = compute_brier_score(raw_probabilities, targets)
        bs_cal = compute_brier_score(calibrated, targets)

        n = len(targets)
        base_rate = (sum(targets) / n) if n > 0 else 0.5
        bs_ref = max(base_rate * (1.0 - base_rate), 1e-6)
        bss = 1.0 - (bs_cal / bs_ref)

        _, ece_raw = build_reliability_diagram(raw_probabilities, targets)
        bins_cal, ece_cal = build_reliability_diagram(calibrated, targets)

        return CalibrationReport(
            head_name=head_name,
            method=self.method,
            temperature=self.temperature,
            brier_score_raw=bs_raw,
            brier_score_calibrated=bs_cal,
            brier_skill_score=bss,
            ece_raw=ece_raw,
            ece_calibrated=ece_cal,
            reliability_bins=bins_cal,
        )
