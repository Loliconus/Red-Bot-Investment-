"""Этапы 1 и 2: Вероятностная модель CatBoost с 3 головами и TreeSHAP (раздел 4 ТЗ).

Оценивает одновременно три калиброванные вероятности:
1. ``P(trend)``          — вероятность направленного режима против шума/боковика;
2. ``P(up | trend)``     — условная вероятность движения вверх при наличии тренда;
3. ``P(break within H)`` — вероятность слома/разворота текущего тренда на горизонте ``H`` баров.

Архитектурные особенности (раздел 4 ТЗ):
- Симметричные (oblivious) деревья решений глубины ``depth = 4..6`` (по умолчанию 5):
  на каждом уровне глубины ``d`` используется единый предикат ``x[f_d] > threshold_d``,
  что служит естественным регуляризатором против переобучения на рыночном шуме.
- Повышенная L2-регуляризация листьев ``l2_leaf_reg``, малый шаг ``learning_rate = 0.01..0.05``,
  обязательный ``early_stopping_rounds`` по валидационному фолду.
- Балансировка редких классов ``auto_class_weights="Balanced"`` и байесовский бутстрэп
  ``bootstrap_type="Bayesian"``.
- Нативная обработка категориальных признаков (``ticker``, ``class_code``, ``day_of_week``)
  через упорядоченные целевые статистики (Ordered Target Statistics) без утечки таргета.
- Точный расчёт SHAP-значений ``get_feature_importance(type="ShapValues")`` по симметричным
  деревьям (свойство аддитивности ``sum(phi_j) + base_value == margin``).
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.synthetic.calibration import CalibrationReport, ProbabilityCalibrator


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        ez = math.exp(-min(z, 60.0))
        return 1.0 / (1.0 + ez)
    ez = math.exp(max(z, -60.0))
    return ez / (1.0 + ez)


def _logit(p: float) -> float:
    pc = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(pc / (1.0 - pc))


@dataclass(frozen=True, slots=True, kw_only=True)
class CatBoostHyperparameters:
    """Гиперпараметры градиентного бустинга по разделу 4 (Этапы 1 и 2) ТЗ."""

    depth: int = 4
    iterations: int = 36
    learning_rate: float = 0.035
    l2_leaf_reg: float = 6.0
    early_stopping_rounds: int = 12
    auto_class_weights: str = "Balanced"
    bootstrap_type: str = "Bayesian"
    bagging_temperature: float = 0.8
    cat_features: tuple[str, ...] = ("ticker", "class_code", "day_of_week")
    calibration_method: str = "hybrid"
    random_seed: int = 42


@dataclass(frozen=True, slots=True, kw_only=True)
class ObliviousTree:
    """Симметричное (oblivious) дерево решений CatBoost глубины ``D``.

    Лист определяется битовой маской длины ``D``:
    ``leaf_idx = sum((1 << d) if x[feature_indices[d]] > thresholds[d] else 0)``.
    """

    feature_indices: tuple[int, ...]
    thresholds: tuple[float, ...]
    leaf_values: tuple[float, ...]
    leaf_weights: tuple[float, ...]

    def predict_margin(self, row: Sequence[float]) -> float:
        leaf_idx = 0
        for d, f_idx in enumerate(self.feature_indices):
            if row[f_idx] > self.thresholds[d]:
                leaf_idx |= 1 << d
        return self.leaf_values[leaf_idx]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbabilisticTriadPrediction:
    """Выход 3 вероятностных голов «Синтетического трейдера» на одном баре."""

    p_trend: float
    p_up_given_trend: float
    p_break_within_h: float
    raw_p_trend: float
    raw_p_up_given_trend: float
    raw_p_break_within_h: float
    expected_directional_edge: float
    confidence_adjusted_edge: float
    top_shap_contributors: Mapping[str, float]


class OrderedTargetEncoder:
    """Упорядоченное кодирование категориальных признаков (Ordered Target Statistics CatBoost).

    Во время обучения для наблюдения ``i`` используется только история ``0 .. i-1``,
    что исключает target leakage. На инференсе используются накопленные счётчики.
    """

    def __init__(self, prior_weight: float = 10.0) -> None:
        self.prior_weight = prior_weight
        self.global_prior: float = 0.5
        self._counts: dict[tuple[str, str], int] = {}
        self._sums: dict[tuple[str, str], float] = {}

    def fit_transform(
        self,
        cat_rows: Sequence[Mapping[str, str]],
        targets: Sequence[int],
        cat_feature_names: Sequence[str],
    ) -> list[list[float]]:
        n = len(cat_rows)
        if n == 0:
            return []
        self.global_prior = sum(targets) / n
        self._counts.clear()
        self._sums.clear()

        encoded: list[list[float]] = []
        for i in range(n):
            row_map = cat_rows[i]
            vec: list[float] = []
            for c_name in cat_feature_names:
                c_val = str(row_map.get(c_name, "NA"))
                key = (c_name, c_val)
                cnt = self._counts.get(key, 0)
                s_val = self._sums.get(key, 0.0)
                stat = (s_val + self.prior_weight * self.global_prior) / (cnt + self.prior_weight)
                vec.append(stat - self.global_prior)
                # Обновляем счётчик строго ПОСЛЕ вычисления признака для i-го сэмпла
                self._counts[key] = cnt + 1
                self._sums[key] = s_val + float(targets[i])
            encoded.append(vec)
        return encoded

    def transform(
        self,
        cat_rows: Sequence[Mapping[str, str]],
        cat_feature_names: Sequence[str],
    ) -> list[list[float]]:
        encoded: list[list[float]] = []
        for row_map in cat_rows:
            vec: list[float] = []
            for c_name in cat_feature_names:
                c_val = str(row_map.get(c_name, "NA"))
                key = (c_name, c_val)
                cnt = self._counts.get(key, 0)
                s_val = self._sums.get(key, 0.0)
                stat = (s_val + self.prior_weight * self.global_prior) / (cnt + self.prior_weight)
                vec.append(stat - self.global_prior)
            encoded.append(vec)
        return encoded


def _bayesian_bootstrap_weights(
    n_samples: int,
    tree_idx: int,
    seed: int,
    temperature: float,
) -> list[float]:
    """Генерирует детерминированные веса байесовского бутстрэпа ``(-ln u_i)^temperature``."""
    if temperature <= 0.0:
        return [1.0] * n_samples
    weights: list[float] = []
    state = (seed * 1_664_525 + tree_idx * 1_013_904_223 + 17) & 0xFFFFFFFF
    for _ in range(n_samples):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        u = max((state + 1.0) / 4_294_967_297.0, 1e-6)
        w = (-math.log(u)) ** temperature
        weights.append(min(w, 6.0))
    return weights


def _precompute_candidate_thresholds(
    matrix: Sequence[Sequence[float]],
    max_bins: int = 8,
) -> list[tuple[float, ...]]:
    """Предвычисляет квантильные пороги разбиений (квантизация признаков как в CatBoost)."""
    if not matrix or not matrix[0]:
        return []
    n_samples = len(matrix)
    n_features = len(matrix[0])
    thresholds_per_feature: list[tuple[float, ...]] = []

    for f_idx in range(n_features):
        col = sorted(matrix[i][f_idx] for i in range(n_samples))
        unique_cuts: list[float] = []
        for q in range(1, max_bins + 1):
            pos = min(n_samples - 1, int(n_samples * q / (max_bins + 1)))
            val = col[pos]
            if not unique_cuts or abs(val - unique_cuts[-1]) > 1e-9:
                unique_cuts.append(val)
        if not unique_cuts:
            unique_cuts.append(0.0)
        thresholds_per_feature.append(tuple(unique_cuts))
    return thresholds_per_feature


class SingleHeadCatBoostClassifier:
    """Одиночная вероятностная голова на симметричных деревьях (Oblivious GBDT + Калибратор).

    Поддерживает как встроенный движок симметричных деревьев, так и нативный
    ``catboost.CatBoostClassifier`` при наличии пакета ``catboost`` в окружении.
    """

    def __init__(
        self,
        head_name: str,
        params: CatBoostHyperparameters | None = None,
    ) -> None:
        self.head_name = head_name
        self.params = params or CatBoostHyperparameters()
        self.feature_names: tuple[str, ...] = ()
        self.all_feature_names: tuple[str, ...] = ()
        self.encoder = OrderedTargetEncoder()
        self.calibrator = ProbabilityCalibrator(method=self.params.calibration_method)
        self.base_margin: float = 0.0
        self.trees: tuple[ObliviousTree, ...] = ()
        self.best_iteration: int = 0
        self.validation_log_loss: float = 0.0
        self._native_model: object | None = None
        self.backend_name: str = "Oblivious GBDT (Symmetric Trees)"

    def fit(
        self,
        feature_names: Sequence[str],
        train_x: Sequence[Sequence[float]],
        train_cat: Sequence[Mapping[str, str]],
        train_y: Sequence[int],
        *,
        val_x: Sequence[Sequence[float]] | None = None,
        val_cat: Sequence[Mapping[str, str]] | None = None,
        val_y: Sequence[int] | None = None,
    ) -> SingleHeadCatBoostClassifier:
        """Обучает ансамбль симметричных деревьев с ранней остановкой и пост-калибровкой."""
        self.feature_names = tuple(feature_names)
        cat_names = self.params.cat_features
        self.all_feature_names = self.feature_names + tuple(f"cat_{c}" for c in cat_names)

        n_train = len(train_x)
        if n_train == 0:
            self.base_margin = 0.0
            self.trees = ()
            return self

        pos_count = sum(1 for y in train_y if y == 1)
        neg_count = n_train - pos_count
        pos_rate = min(max(pos_count / n_train, 0.02), 0.98)
        self.base_margin = _logit(pos_rate)

        # Веса классов auto_class_weights="Balanced"
        if self.params.auto_class_weights == "Balanced" and pos_count > 0 and neg_count > 0:
            w_pos = n_train / (2.0 * pos_count)
            w_neg = n_train / (2.0 * neg_count)
        else:
            w_pos = 1.0
            w_neg = 1.0

        train_cat_enc = self.encoder.fit_transform(train_cat, train_y, cat_names)
        full_train_x = [list(train_x[i]) + train_cat_enc[i] for i in range(n_train)]

        if val_x is not None and val_cat is not None and val_y is not None and len(val_x) > 0:
            val_cat_enc = self.encoder.transform(val_cat, cat_names)
            full_val_x = [list(val_x[i]) + val_cat_enc[i] for i in range(len(val_x))]
            effective_val_y = list(val_y)
            fit_indices = list(range(n_train))
        elif n_train >= 20:
            # Стратифицированный по времени OOF-фолд (каждый 5-й бар из всех фаз рынка),
            # чтобы валидация и калибровка охватывали и TREND, и CHOP, и PANIC
            val_indices = [i for i in range(n_train) if i % 5 == 4]
            fit_indices = [i for i in range(n_train) if i % 5 != 4]
            full_val_x = [full_train_x[i] for i in val_indices]
            effective_val_y = [train_y[i] for i in val_indices]
        else:
            split_idx = max(1, int(n_train * 0.8))
            fit_indices = list(range(n_train))
            full_val_x = full_train_x[split_idx:]
            effective_val_y = list(train_y[split_idx:])

        n_total_features = len(self.all_feature_names)
        thresholds_by_f = _precompute_candidate_thresholds(full_train_x, max_bins=6)
        depth = max(1, min(self.params.depth, 6))
        lr = self.params.learning_rate
        l2_reg = self.params.l2_leaf_reg

        train_margins = [self.base_margin] * n_train
        val_margins = [self.base_margin] * len(full_val_x)

        built_trees: list[ObliviousTree] = []
        best_val_loss = 1e18
        best_tree_count = 0
        rounds_without_improve = 0

        for it in range(self.params.iterations):
            boot_w = (
                _bayesian_bootstrap_weights(
                    n_train,
                    it,
                    self.params.random_seed,
                    self.params.bagging_temperature,
                )
                if self.params.bootstrap_type == "Bayesian"
                else [1.0] * n_train
            )

            grad = [0.0] * n_train
            hess = [0.0] * n_train
            for i in fit_indices:
                p_i = _sigmoid(train_margins[i])
                cw = w_pos if train_y[i] == 1 else w_neg
                bw = boot_w[i] * cw
                grad[i] = bw * (float(train_y[i]) - p_i)
                hess[i] = bw * max(p_i * (1.0 - p_i), 1e-5)

            # Строим симметричное дерево жадно по уровням глубины d = 0 .. depth-1
            leaf_indices = [0] * n_train
            chosen_features: list[int] = []
            chosen_thresholds: list[float] = []

            for d in range(depth):
                n_leaves_next = 1 << (d + 1)
                best_gain = -1e18
                best_f = 0
                best_thr = 0.0

                # Перебираем признаки и квантильные пороги только по обучающим индексам fit_indices
                for f_idx in range(n_total_features):
                    for thr in thresholds_by_f[f_idx]:
                        g_buckets = [0.0] * n_leaves_next
                        h_buckets = [0.0] * n_leaves_next
                        for i in fit_indices:
                            next_leaf = leaf_indices[i] | (
                                (1 << d) if full_train_x[i][f_idx] > thr else 0
                            )
                            g_buckets[next_leaf] += grad[i]
                            h_buckets[next_leaf] += hess[i]

                        gain = sum(
                            (g_buckets[b] * g_buckets[b]) / (h_buckets[b] + l2_reg)
                            for b in range(n_leaves_next)
                        )
                        if gain > best_gain:
                            best_gain = gain
                            best_f = f_idx
                            best_thr = thr

                chosen_features.append(best_f)
                chosen_thresholds.append(best_thr)
                for i in range(n_train):
                    if full_train_x[i][best_f] > best_thr:
                        leaf_indices[i] |= 1 << d

            n_final_leaves = 1 << depth
            g_leaf = [0.0] * n_final_leaves
            h_leaf = [0.0] * n_final_leaves
            c_leaf = [0.0] * n_final_leaves
            for i in fit_indices:
                lid = leaf_indices[i]
                g_leaf[lid] += grad[i]
                h_leaf[lid] += hess[i]
                c_leaf[lid] += 1.0

            total_c = max(sum(c_leaf), 1.0)
            leaf_vals = tuple(
                lr * (g_leaf[b] / (h_leaf[b] + l2_reg)) for b in range(n_final_leaves)
            )
            leaf_wts = tuple(max(c_leaf[b] / total_c, 1e-4) for b in range(n_final_leaves))

            tree = ObliviousTree(
                feature_indices=tuple(chosen_features),
                thresholds=tuple(chosen_thresholds),
                leaf_values=leaf_vals,
                leaf_weights=leaf_wts,
            )
            built_trees.append(tree)

            for i in range(n_train):
                train_margins[i] += leaf_vals[leaf_indices[i]]

            # Оценка на валидационном фолде и early stopping
            val_loss = 0.0
            for j, v_row in enumerate(full_val_x):
                val_margins[j] += tree.predict_margin(v_row)
                pv = min(max(_sigmoid(val_margins[j]), 1e-6), 1.0 - 1e-6)
                yv = float(effective_val_y[j])
                val_loss -= yv * math.log(pv) + (1.0 - yv) * math.log(1.0 - pv)
            val_loss /= max(len(full_val_x), 1)

            if val_loss < best_val_loss - 1e-5:
                best_val_loss = val_loss
                best_tree_count = len(built_trees)
                rounds_without_improve = 0
            else:
                rounds_without_improve += 1
                if rounds_without_improve >= self.params.early_stopping_rounds:
                    break

        self.best_iteration = max(1, best_tree_count)
        kept_trees = built_trees[: self.best_iteration]
        centered_trees: list[ObliviousTree] = []
        for t in kept_trees:
            w_sum = sum(t.leaf_weights)
            norm_w = tuple(w / w_sum for w in t.leaf_weights) if w_sum > 0 else t.leaf_weights
            t_mean = sum(norm_w[b] * t.leaf_values[b] for b in range(len(norm_w)))
            self.base_margin += t_mean
            centered_trees.append(
                ObliviousTree(
                    feature_indices=t.feature_indices,
                    thresholds=t.thresholds,
                    leaf_values=tuple(v - t_mean for v in t.leaf_values),
                    leaf_weights=norm_w,
                )
            )
        self.trees = tuple(centered_trees)
        self.validation_log_loss = best_val_loss

        # Синхронизация с нативным CatBoostClassifier / LightGBM / HistGBDT при наличии в среде
        fit_sub_x = [full_train_x[i] for i in fit_indices]
        fit_sub_y = [train_y[i] for i in fit_indices]
        self._try_fit_native_catboost(fit_sub_x, fit_sub_y, full_val_x, effective_val_y)

        # Пост-калибровка на валидационном фолде
        raw_val_probs = [self.predict_raw_proba_row(row) for row in full_val_x]
        self.calibrator.fit(raw_val_probs, effective_val_y)
        return self

    def _try_fit_native_catboost(
        self,
        train_x: Sequence[Sequence[float]],
        train_y: Sequence[int],
        val_x: Sequence[Sequence[float]],
        val_y: Sequence[int],
    ) -> None:
        """Обучает нативный C++ ``catboost.CatBoostClassifier`` (или ``HistGradientBoosting``)."""
        if len(set(train_y)) < 2:
            self._native_model = None
            return

        # 1. Нативный CatBoost C++ (колесо cp313/cp314)
        try:
            cb_mod = importlib.import_module("catboost")
            cls = cb_mod.CatBoostClassifier(
                depth=self.params.depth,
                iterations=self.params.iterations,
                learning_rate=self.params.learning_rate,
                l2_leaf_reg=self.params.l2_leaf_reg,
                early_stopping_rounds=self.params.early_stopping_rounds,
                auto_class_weights=self.params.auto_class_weights,
                bootstrap_type=self.params.bootstrap_type,
                bagging_temperature=self.params.bagging_temperature,
                random_seed=self.params.random_seed,
                verbose=False,
                allow_writing_files=False,
            )
            cls.fit(
                list(train_x),
                list(train_y),
                eval_set=(list(val_x), list(val_y)),
                verbose=False,
            )
            self._native_model = cls
            self.backend_name = "CatBoost C++ (native) + TreeSHAP"
            return
        except (ImportError, RuntimeError, ValueError, TypeError):
            self._native_model = None

        # 2. Резервный Cython-бустер scikit-learn HistGradientBoostingClassifier
        try:
            sk_ens = importlib.import_module("sklearn.ensemble")
            hgb = sk_ens.HistGradientBoostingClassifier(
                max_depth=self.params.depth,
                max_iter=self.params.iterations,
                learning_rate=self.params.learning_rate,
                l2_regularization=self.params.l2_leaf_reg,
                min_samples_leaf=max(3, len(train_x) // 12),
                random_state=self.params.random_seed,
            )
            hgb.fit(list(train_x), list(train_y))
            self._native_model = hgb
            self.backend_name = "HistGradientBoosting (scikit-learn) + Oblivious TreeSHAP"
        except (ImportError, RuntimeError, ValueError, TypeError):
            self._native_model = None
            self.backend_name = "Oblivious GBDT (Symmetric Trees)"

    def encode_row(
        self,
        numeric_row: Sequence[float],
        categorical_row: Mapping[str, str],
    ) -> list[float]:
        cat_enc = self.encoder.transform([categorical_row], self.params.cat_features)[0]
        return list(numeric_row) + cat_enc

    def predict_margin_row(self, encoded_row: Sequence[float]) -> float:
        margin = self.base_margin
        for tree in self.trees:
            margin += tree.predict_margin(encoded_row)
        return margin

    def predict_raw_proba_row(self, encoded_row: Sequence[float]) -> float:
        oblivious_p = _sigmoid(self.predict_margin_row(encoded_row))
        if self._native_model is not None:
            predict_proba = getattr(self._native_model, "predict_proba", None)
            if callable(predict_proba):
                try:
                    probs = predict_proba([list(encoded_row)])
                    native_p = float(probs[0][1])
                    return 0.5 * native_p + 0.5 * oblivious_p
                except (RuntimeError, ValueError, TypeError, IndexError):
                    return oblivious_p
        return oblivious_p

    def predict_calibrated_proba(
        self,
        numeric_row: Sequence[float],
        categorical_row: Mapping[str, str],
    ) -> tuple[float, float]:
        """Возвращает ``(calibrated_prob, raw_prob)``."""
        encoded = self.encode_row(numeric_row, categorical_row)
        raw_p = self.predict_raw_proba_row(encoded)
        cal_p = self.calibrator.calibrate_one(raw_p)
        return cal_p, raw_p

    def compute_shap_values_row(
        self,
        numeric_row: Sequence[float],
        categorical_row: Mapping[str, str],
    ) -> dict[str, float]:
        """Вычисляет точные TreeSHAP-вклады признаков для симметричных деревьев.

        Для каждого симметричного дерева глубины ``D`` (где ``D <= 6``) выполняет
        точный расчёт значений Шепли по ``2^D`` коалициям уровней дерева.
        Гарантирует точное тождество Шепли:
        ``sum(shap_values.values()) + base_margin == predict_margin_row(encoded_row)``.
        """
        encoded = self.encode_row(numeric_row, categorical_row)
        n_total = len(self.all_feature_names)
        phi = [0.0] * n_total

        for tree in self.trees:
            d_depth = len(tree.feature_indices)
            if d_depth == 0:
                continue
            n_subsets = 1 << d_depth
            n_leaves = 1 << d_depth

            # Направления для данного наблюдения на каждом уровне d: 0 или 1
            sample_bits = [
                1 if encoded[tree.feature_indices[d]] > tree.thresholds[d] else 0
                for d in range(d_depth)
            ]

            # Для каждого подмножества известных уровней mask in 0 .. 2^D - 1
            # считаем условное матожидание листа E[v | x_S]
            cond_exp = [0.0] * n_subsets
            for s_mask in range(n_subsets):
                num = 0.0
                den = 0.0
                for leaf_id in range(n_leaves):
                    # Проверяем совместимость листа с известными уровнями из s_mask
                    compatible = True
                    for d in range(d_depth):
                        if (s_mask & (1 << d)) and (((leaf_id >> d) & 1) != sample_bits[d]):
                            compatible = False
                            break
                    if compatible:
                        w = tree.leaf_weights[leaf_id]
                        num += w * tree.leaf_values[leaf_id]
                        den += w
                cond_exp[s_mask] = num / den if den > 1e-12 else 0.0

            # Веса коалиций Шепли |S|! * (D - |S| - 1)! / D!
            fact = [math.factorial(k) for k in range(d_depth + 1)]
            d_fact = fact[d_depth]

            for d in range(d_depth):
                bit_d = 1 << d
                contrib_d = 0.0
                for s_mask in range(n_subsets):
                    if s_mask & bit_d:
                        continue
                    s_size = s_mask.bit_count()
                    shap_w = (fact[s_size] * fact[d_depth - s_size - 1]) / d_fact
                    contrib_d += shap_w * (cond_exp[s_mask | bit_d] - cond_exp[s_mask])
                phi[tree.feature_indices[d]] += contrib_d

        return {self.all_feature_names[j]: phi[j] for j in range(n_total)}

    def get_feature_importance(
        self,
        numeric_rows: Sequence[Sequence[float]],
        categorical_rows: Sequence[Mapping[str, str]],
        *,
        importance_type: str = "ShapValues",
    ) -> dict[str, float]:
        """Агрегирует средние модули SHAP-значений (``type='ShapValues'`` по разделу 3.3 ТЗ)."""
        if importance_type != "ShapValues":
            msg = f"Поддерживается importance_type='ShapValues', получено: {importance_type}"
            raise ValueError(msg)
        if not numeric_rows:
            return {name: 0.0 for name in self.all_feature_names}

        totals = {name: 0.0 for name in self.all_feature_names}
        stride = max(1, len(numeric_rows) // 24)
        sampled_indices = range(0, len(numeric_rows), stride)
        count = 0
        for i in sampled_indices:
            row = numeric_rows[i]
            cat_row = categorical_rows[i] if i < len(categorical_rows) else {}
            row_shap = self.compute_shap_values_row(row, cat_row)
            for k, val in row_shap.items():
                totals[k] = totals.get(k, 0.0) + abs(val)
            count += 1

        n = max(count, 1)
        return {k: v / n for k, v in totals.items()}


class SyntheticCatBoostTriadModel:
    """Трёхголовая вероятностная модель CatBoost «Синтетического трейдера».

    Головы:
    - ``head_trend``: оценка ``P(trend)``
    - ``head_up``:    оценка ``P(up | trend)`` (обучается на направленных барах)
    - ``head_break``: оценка ``P(break within H)``
    """

    def __init__(self, params: CatBoostHyperparameters | None = None) -> None:
        self.params = params or CatBoostHyperparameters()
        self.head_trend = SingleHeadCatBoostClassifier("P(trend)", self.params)
        self.head_up = SingleHeadCatBoostClassifier("P(up|trend)", self.params)
        self.head_break = SingleHeadCatBoostClassifier("P(break_within_H)", self.params)
        self.feature_names: tuple[str, ...] = ()

    def fit(
        self,
        feature_names: Sequence[str],
        numeric_rows: Sequence[Sequence[float]],
        categorical_rows: Sequence[Mapping[str, str]],
        y_trend: Sequence[int],
        y_up_given_trend: Sequence[int | None],
        y_break_within_h: Sequence[int],
    ) -> SyntheticCatBoostTriadModel:
        """Обучает все 3 вероятностные головы с калибровкой на временном валидационном фолде."""
        self.feature_names = tuple(feature_names)

        # 1. Голова P(trend)
        self.head_trend.fit(
            feature_names,
            numeric_rows,
            categorical_rows,
            y_trend,
        )

        # 2. Голова P(up | trend) — обучается на барах, где реализовалось направленное движение
        up_num: list[Sequence[float]] = []
        up_cat: list[Mapping[str, str]] = []
        up_target: list[int] = []
        for i, y_u in enumerate(y_up_given_trend):
            if y_u is not None:
                up_num.append(numeric_rows[i])
                up_cat.append(categorical_rows[i])
                up_target.append(int(y_u))

        if len(up_num) >= 4:
            self.head_up.fit(feature_names, up_num, up_cat, up_target)
        else:
            fallback_y = [1 if (y_u or 0) > 0 else 0 for y_u in y_up_given_trend]
            self.head_up.fit(feature_names, numeric_rows, categorical_rows, fallback_y)

        # 3. Голова P(break within H)
        self.head_break.fit(
            feature_names,
            numeric_rows,
            categorical_rows,
            y_break_within_h,
        )
        return self

    def predict(
        self,
        numeric_row: Sequence[float],
        categorical_row: Mapping[str, str],
        *,
        compute_shap: bool = True,
    ) -> ProbabilisticTriadPrediction:
        """Вычисляет калиброванную триаду вероятностей и ведущие SHAP-факторы на баре."""
        p_trend, raw_trend = self.head_trend.predict_calibrated_proba(numeric_row, categorical_row)
        p_up, raw_up = self.head_up.predict_calibrated_proba(numeric_row, categorical_row)
        p_break, raw_break = self.head_break.predict_calibrated_proba(
            numeric_row,
            categorical_row,
        )

        # Ожидаемое направленное преимущество E[dir] = P(trend) * (2 * P(up|trend) - 1)
        directional_edge = p_trend * (2.0 * p_up - 1.0)
        # С учётом вероятности слома режима на горизонте H
        confidence_edge = directional_edge * (1.0 - p_break)

        top_shap: dict[str, float] = {}
        if compute_shap:
            shap_trend = self.head_trend.compute_shap_values_row(numeric_row, categorical_row)
            shap_up = self.head_up.compute_shap_values_row(numeric_row, categorical_row)
            combined_shap = {
                k: 0.5 * shap_trend.get(k, 0.0) + 0.5 * shap_up.get(k, 0.0) for k in shap_trend
            }
            top_keys = sorted(combined_shap, key=lambda k: abs(combined_shap[k]), reverse=True)[:8]
            top_shap = {k: combined_shap[k] for k in top_keys}

        return ProbabilisticTriadPrediction(
            p_trend=p_trend,
            p_up_given_trend=p_up,
            p_break_within_h=p_break,
            raw_p_trend=raw_trend,
            raw_p_up_given_trend=raw_up,
            raw_p_break_within_h=raw_break,
            expected_directional_edge=directional_edge,
            confidence_adjusted_edge=confidence_edge,
            top_shap_contributors=top_shap,
        )

    def evaluate_calibration(
        self,
        numeric_rows: Sequence[Sequence[float]],
        categorical_rows: Sequence[Mapping[str, str]],
        y_trend: Sequence[int],
        y_up_given_trend: Sequence[int | None],
        y_break_within_h: Sequence[int],
    ) -> dict[str, CalibrationReport]:
        """Возвращает отчёты калибровки (Brier Score, ECE, Reliability Diagram) по 3 головам."""
        raw_trend = [
            self.head_trend.predict_calibrated_proba(numeric_rows[i], categorical_rows[i])[1]
            for i in range(len(numeric_rows))
        ]
        raw_break = [
            self.head_break.predict_calibrated_proba(numeric_rows[i], categorical_rows[i])[1]
            for i in range(len(numeric_rows))
        ]

        up_raw: list[float] = []
        up_targets: list[int] = []
        for i, y_u in enumerate(y_up_given_trend):
            if y_u is not None:
                up_raw.append(
                    self.head_up.predict_calibrated_proba(numeric_rows[i], categorical_rows[i])[1]
                )
                up_targets.append(int(y_u))

        if not up_raw:
            up_raw = [0.5]
            up_targets = [1]

        return {
            "P(trend)": self.head_trend.calibrator.evaluate("P(trend)", raw_trend, y_trend),
            "P(up|trend)": self.head_up.calibrator.evaluate("P(up|trend)", up_raw, up_targets),
            "P(break_within_H)": self.head_break.calibrator.evaluate(
                "P(break_within_H)",
                raw_break,
                y_break_within_h,
            ),
        }
