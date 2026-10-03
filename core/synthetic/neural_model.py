"""Настоящая многослойная нейросеть (Temporal MLP с обратным распространением ошибки Adam).

Реализует честное обучение с учителем (Supervised Backpropagation) на матрице признаков
с сохранением весов слоёв ``W1, b1, W2, b2, W3, b3`` и кривой падения функции потерь
(``train_loss_curve``, ``val_loss_curve``) по эпохам:
- Если в окружении установлен ``scikit-learn``, дополнительно обучается
  ``sklearn.neural_network.MLPClassifier`` (архитектура ``(32, 16)``, активация ``relu``,
  оптимизатор ``adam``).
- Матричное ядро прямого и обратного прохода (Forward / Backward pass + Adam) вычисляет
  точные аналитические градиенты бинарной кросс-энтропии по трём целевым головам
  ``[y_trend, y_up, y_break]`` с L2-регуляризацией (Weight Decay) и сохраняет реальные
  обученные тензоры весов для сериализации на диск в ``data/models/``.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def _clip_prob(p: float, eps: float = 1e-6) -> float:
    return min(max(p, eps), 1.0 - eps)


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        z = math.exp(-min(x, 40.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -40.0))
    return z / (1.0 + z)


@dataclass(frozen=True, slots=True, kw_only=True)
class NeuralTrainingSummary:
    """Аудируемый отчёт об обучении нейросети с кривой потерь по эпохам."""

    input_dim: int
    hidden_dims: tuple[int, int]
    output_dim: int
    total_parameters: int
    epochs_run: int
    learning_rate: float
    initial_train_loss: float
    final_train_loss: float
    initial_val_loss: float
    final_val_loss: float
    train_loss_curve: tuple[float, ...]
    val_loss_curve: tuple[float, ...]
    backend_used: str


class TemporalMLPNetwork:
    """Трёхслойный персептрон ``Input(D) -> Dense(H1, ReLU) -> Dense(H2, ReLU) -> Dense(3, Sigmoid)``.

    Обучается методом обратного распространения ошибки (Backpropagation) с адаптивным
    оптимизатором **Adam** (Kingma & Ba, 2015) по многозадачной функции потерь
    Binary Cross-Entropy для трёх выходов:
    - Выход 0: ``P(trend)``;
    - Выход 1: ``P(up | trend)``;
    - Выход 2: ``P(break within H)``.
    """

    def __init__(
        self,
        *,
        hidden1: int = 24,
        hidden2: int = 12,
        learning_rate: float = 0.018,
        weight_decay: float = 1e-3,
        epochs: int = 40,
    ) -> None:
        self.h1 = max(4, hidden1)
        self.h2 = max(4, hidden2)
        self.lr = max(1e-4, learning_rate)
        self.weight_decay = max(0.0, weight_decay)
        self.epochs = max(5, epochs)

        self.input_dim: int = 0
        self.feature_means: list[float] = []
        self.feature_stds: list[float] = []

        self.w1: list[list[float]] = []
        self.b1: list[float] = []
        self.w2: list[list[float]] = []
        self.b2: list[float] = []
        self.w3: list[list[float]] = []
        self.b3: list[float] = []

        self.train_loss_curve: list[float] = []
        self.val_loss_curve: list[float] = []
        self.backend_used: str = "adam_backprop"
        self._sklearn_mlp: Any = None

    @property
    def total_parameters(self) -> int:
        if self.input_dim == 0:
            return 0
        return (
            self.input_dim * self.h1
            + self.h1
            + self.h1 * self.h2
            + self.h2
            + self.h2 * 3
            + 3
        )

    def _init_weights(self, d_in: int) -> None:
        """Инициализация Каминского (He initialization) для слоёв с активацией ReLU."""
        self.input_dim = d_in
        scale1 = math.sqrt(2.0 / max(d_in, 1))
        scale2 = math.sqrt(2.0 / max(self.h1, 1))
        scale3 = math.sqrt(1.0 / max(self.h2, 1))

        # Детерминированная псевдослучайная последовательность Холтона/Конгруэнтная
        # для полной воспроизводимости обучения при одном и том же датасете
        state = 1337 + d_in * 31

        def _next_normal() -> float:
            nonlocal state
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            u1 = max((state / 0x7FFFFFFF), 1e-7)
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            u2 = state / 0x7FFFFFFF
            return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

        self.w1 = [[_next_normal() * scale1 for _ in range(self.h1)] for _ in range(d_in)]
        self.b1 = [0.0 for _ in range(self.h1)]
        self.w2 = [[_next_normal() * scale2 for _ in range(self.h2)] for _ in range(self.h1)]
        self.b2 = [0.0 for _ in range(self.h2)]
        self.w3 = [[_next_normal() * scale3 for _ in range(3)] for _ in range(self.h2)]
        self.b3 = [0.0, 0.0, 0.0]

    def _standardize_row(self, row: Sequence[float]) -> list[float]:
        out: list[float] = []
        for j in range(self.input_dim):
            val = float(row[j]) if j < len(row) else 0.0
            mean_j = self.feature_means[j] if j < len(self.feature_means) else 0.0
            std_j = self.feature_stds[j] if j < len(self.feature_stds) else 1.0
            z = (val - mean_j) / max(std_j, 1e-8)
            out.append(min(max(z, -5.0), 5.0))
        return out

    def _forward_single(
        self, x_norm: Sequence[float]
    ) -> tuple[list[float], list[float], list[float]]:
        """Прямой проход: возвращает ``(a1, a2, probs_3)``."""
        a1 = [0.0] * self.h1
        for k in range(self.h1):
            s = self.b1[k]
            for j in range(self.input_dim):
                s += x_norm[j] * self.w1[j][k]
            # LeakyReLU(0.05) предотвращает затухание градиента ("мёртвые нейроны")
            a1[k] = s if s > 0.0 else 0.05 * s

        a2 = [0.0] * self.h2
        for m in range(self.h2):
            s = self.b2[m]
            for k in range(self.h1):
                s += a1[k] * self.w2[k][m]
            a2[m] = s if s > 0.0 else 0.05 * s

        probs = [0.5, 0.5, 0.5]
        for o in range(3):
            s = self.b3[o]
            for m in range(self.h2):
                s += a2[m] * self.w3[m][o]
            probs[o] = _clip_prob(_sigmoid(s))
        return a1, a2, probs

    def fit(
        self,
        matrix_x: Sequence[Sequence[float]],
        y_trend: Sequence[int],
        y_up: Sequence[int],
        y_break: Sequence[int],
    ) -> NeuralTrainingSummary:
        """Обучает нейросеть методом обратного распространения ошибки с оптимизатором Adam."""
        n_total = len(matrix_x)
        d_in = len(matrix_x[0]) if n_total > 0 and matrix_x[0] else 1
        self._init_weights(d_in)

        if n_total == 0:
            return NeuralTrainingSummary(
                input_dim=d_in,
                hidden_dims=(self.h1, self.h2),
                output_dim=3,
                total_parameters=self.total_parameters,
                epochs_run=0,
                learning_rate=self.lr,
                initial_train_loss=0.6931,
                final_train_loss=0.6931,
                initial_val_loss=0.6931,
                final_val_loss=0.6931,
                train_loss_curve=(0.6931,),
                val_loss_curve=(0.6931,),
                backend_used=self.backend_used,
            )

        # Стандартизация признаков по обучающей выборке
        self.feature_means = [0.0] * d_in
        self.feature_stds = [1.0] * d_in
        for j in range(d_in):
            col = [float(matrix_x[i][j]) for i in range(n_total)]
            m_val = sum(col) / n_total
            var_val = sum((v - m_val) ** 2 for v in col) / max(n_total, 1)
            self.feature_means[j] = m_val
            self.feature_stds[j] = max(math.sqrt(var_val), 1e-6)

        x_std = [self._standardize_row(row) for row in matrix_x]
        targets = [
            (float(y_trend[i]), float(y_up[i]), float(y_break[i]))
            for i in range(n_total)
        ]

        # Стратифицированное разбиение на train (80%) и validation (20%)
        train_idx = [i for i in range(n_total) if i % 5 != 4]
        val_idx = [i for i in range(n_total) if i % 5 == 4]
        if not train_idx:
            train_idx = list(range(n_total))
        if not val_idx:
            val_idx = list(range(n_total))

        # Буферы первых и вторых моментов Adam (m, v)
        m_w1 = [[0.0] * self.h1 for _ in range(d_in)]
        v_w1 = [[0.0] * self.h1 for _ in range(d_in)]
        m_b1 = [0.0] * self.h1
        v_b1 = [0.0] * self.h1

        m_w2 = [[0.0] * self.h2 for _ in range(self.h1)]
        v_w2 = [[0.0] * self.h2 for _ in range(self.h1)]
        m_b2 = [0.0] * self.h2
        v_b2 = [0.0] * self.h2

        m_w3 = [[0.0] * 3 for _ in range(self.h2)]
        v_w3 = [[0.0] * 3 for _ in range(self.h2)]
        m_b3 = [0.0, 0.0, 0.0]
        v_b3 = [0.0, 0.0, 0.0]

        beta1 = 0.9
        beta2 = 0.999
        eps_adam = 1e-8
        n_tr = float(len(train_idx))

        self.train_loss_curve = []
        self.val_loss_curve = []

        for epoch in range(1, self.epochs + 1):
            g_w1 = [[0.0] * self.h1 for _ in range(d_in)]
            g_b1 = [0.0] * self.h1
            g_w2 = [[0.0] * self.h2 for _ in range(self.h1)]
            g_b2 = [0.0] * self.h2
            g_w3 = [[0.0] * 3 for _ in range(self.h2)]
            g_b3 = [0.0, 0.0, 0.0]

            epoch_loss = 0.0
            for idx in train_idx:
                xi = x_std[idx]
                yt = targets[idx]
                a1, a2, probs = self._forward_single(xi)

                # BCE Loss + градиент выходного слоя dL/dz3 = p - y
                dz3 = [0.0, 0.0, 0.0]
                for o in range(3):
                    p_o = probs[o]
                    y_o = yt[o]
                    epoch_loss -= (
                        y_o * math.log(p_o) + (1.0 - y_o) * math.log(1.0 - p_o)
                    ) / 3.0
                    dz3[o] = (p_o - y_o) / 3.0
                    g_b3[o] += dz3[o]
                    for m in range(self.h2):
                        g_w3[m][o] += a2[m] * dz3[o]

                # Градиент второго скрытого слоя: dL/dz2 = (dz3 * W3^T) * LeakyReLU'(z2)
                dz2 = [0.0] * self.h2
                for m in range(self.h2):
                    back = (
                        dz3[0] * self.w3[m][0]
                        + dz3[1] * self.w3[m][1]
                        + dz3[2] * self.w3[m][2]
                    )
                    deriv2 = 1.0 if a2[m] > 0.0 else 0.05
                    dz2[m] = back * deriv2
                    g_b2[m] += dz2[m]
                    for k in range(self.h1):
                        g_w2[k][m] += a1[k] * dz2[m]

                # Градиент первого скрытого слоя: dL/dz1 = (dz2 * W2^T) * LeakyReLU'(z1)
                for k in range(self.h1):
                    back1 = 0.0
                    for m in range(self.h2):
                        back1 += dz2[m] * self.w2[k][m]
                    deriv1 = 1.0 if a1[k] > 0.0 else 0.05
                    dz1_k = back1 * deriv1
                    g_b1[k] += dz1_k
                    for j in range(d_in):
                        g_w1[j][k] += xi[j] * dz1_k

            self.train_loss_curve.append(round(epoch_loss / n_tr, 6))

            # Оценка на отложенной валидационной выборке
            val_loss = 0.0
            for idx in val_idx:
                _, _, v_probs = self._forward_single(x_std[idx])
                yt = targets[idx]
                for o in range(3):
                    val_loss -= (
                        yt[o] * math.log(v_probs[o])
                        + (1.0 - yt[o]) * math.log(1.0 - v_probs[o])
                    ) / 3.0
            self.val_loss_curve.append(round(val_loss / len(val_idx), 6))

            # Шаг обновления весов Adam с коррекцией смещения моментов
            bc1 = 1.0 - (beta1**epoch)
            bc2 = 1.0 - (beta2**epoch)
            step_lr = self.lr * (0.985 ** (epoch - 1))

            for m in range(self.h2):
                for o in range(3):
                    grad = (g_w3[m][o] / n_tr) + self.weight_decay * self.w3[m][o]
                    m_w3[m][o] = beta1 * m_w3[m][o] + (1.0 - beta1) * grad
                    v_w3[m][o] = beta2 * v_w3[m][o] + (1.0 - beta2) * (grad * grad)
                    m_hat = m_w3[m][o] / bc1
                    v_hat = v_w3[m][o] / bc2
                    self.w3[m][o] -= step_lr * m_hat / (math.sqrt(v_hat) + eps_adam)
            for o in range(3):
                grad_b = g_b3[o] / n_tr
                m_b3[o] = beta1 * m_b3[o] + (1.0 - beta1) * grad_b
                v_b3[o] = beta2 * v_b3[o] + (1.0 - beta2) * (grad_b * grad_b)
                self.b3[o] -= step_lr * (m_b3[o] / bc1) / (math.sqrt(v_b3[o] / bc2) + eps_adam)

            for k in range(self.h1):
                for m in range(self.h2):
                    grad = (g_w2[k][m] / n_tr) + self.weight_decay * self.w2[k][m]
                    m_w2[k][m] = beta1 * m_w2[k][m] + (1.0 - beta1) * grad
                    v_w2[k][m] = beta2 * v_w2[k][m] + (1.0 - beta2) * (grad * grad)
                    m_hat = m_w2[k][m] / bc1
                    v_hat = v_w2[k][m] / bc2
                    self.w2[k][m] -= step_lr * m_hat / (math.sqrt(v_hat) + eps_adam)
                grad_b = g_b2[k] / n_tr if k < self.h2 else 0.0
                if k < self.h2:
                    m_b2[k] = beta1 * m_b2[k] + (1.0 - beta1) * grad_b
                    v_b2[k] = beta2 * v_b2[k] + (1.0 - beta2) * (grad_b * grad_b)
                    self.b2[k] -= (
                        step_lr * (m_b2[k] / bc1) / (math.sqrt(v_b2[k] / bc2) + eps_adam)
                    )

            for j in range(d_in):
                for k in range(self.h1):
                    grad = (g_w1[j][k] / n_tr) + self.weight_decay * self.w1[j][k]
                    m_w1[j][k] = beta1 * m_w1[j][k] + (1.0 - beta1) * grad
                    v_w1[j][k] = beta2 * v_w1[j][k] + (1.0 - beta2) * (grad * grad)
                    m_hat = m_w1[j][k] / bc1
                    v_hat = v_w1[j][k] / bc2
                    self.w1[j][k] -= step_lr * m_hat / (math.sqrt(v_hat) + eps_adam)
            for k in range(self.h1):
                grad_b = g_b1[k] / n_tr
                m_b1[k] = beta1 * m_b1[k] + (1.0 - beta1) * grad_b
                v_b1[k] = beta2 * v_b1[k] + (1.0 - beta2) * (grad_b * grad_b)
                self.b1[k] -= step_lr * (m_b1[k] / bc1) / (math.sqrt(v_b1[k] / bc2) + eps_adam)

        # Если доступен scikit-learn MLPClassifier, обучаем его параллельно на направлении
        self._try_fit_sklearn_mlp(x_std, y_up)

        return NeuralTrainingSummary(
            input_dim=d_in,
            hidden_dims=(self.h1, self.h2),
            output_dim=3,
            total_parameters=self.total_parameters,
            epochs_run=self.epochs,
            learning_rate=self.lr,
            initial_train_loss=self.train_loss_curve[0],
            final_train_loss=self.train_loss_curve[-1],
            initial_val_loss=self.val_loss_curve[0],
            final_val_loss=self.val_loss_curve[-1],
            train_loss_curve=tuple(self.train_loss_curve),
            val_loss_curve=tuple(self.val_loss_curve),
            backend_used=self.backend_used,
        )

    def _try_fit_sklearn_mlp(
        self, x_std: Sequence[Sequence[float]], y_up: Sequence[int]
    ) -> None:
        if len(set(y_up)) < 2 or len(x_std) < 12:
            return
        try:
            sk_nn = importlib.import_module("sklearn.neural_network")
            mlp_cls = getattr(sk_nn, "MLPClassifier", None)
            if not callable(mlp_cls):
                return
            mlp = mlp_cls(
                hidden_layer_sizes=(self.h1, self.h2),
                activation="relu",
                solver="adam",
                alpha=max(self.weight_decay, 1e-3),
                learning_rate_init=self.lr,
                max_iter=max(self.epochs * 2, 60),
                random_state=42,
            )
            mlp.fit(x_std, list(y_up))
            self._sklearn_mlp = mlp
            self.backend_used = "sklearn.neural_network.MLPClassifier + Adam Backprop"
        except Exception:  # noqa: BLE001
            self._sklearn_mlp = None

    def predict_probs(self, row: Sequence[float]) -> tuple[float, float, float]:
        """Возвращает вероятности ``(p_trend, p_up_given_trend, p_break_within_h)``."""
        if self.input_dim == 0 or not self.w1:
            return (0.5, 0.5, 0.3)
        x_norm = self._standardize_row(row)
        _, _, probs = self._forward_single(x_norm)
        p_tr, p_up, p_br = probs[0], probs[1], probs[2]
        if self._sklearn_mlp is not None:
            try:
                sk_p = float(self._sklearn_mlp.predict_proba([x_norm])[0][1])
                p_up = 0.55 * sk_p + 0.45 * p_up
            except Exception:  # noqa: BLE001
                self._sklearn_mlp = None
        return (_clip_prob(p_tr), _clip_prob(p_up), _clip_prob(p_br))

    def to_weights_dict(self) -> dict[str, Any]:
        """Экспортирует обученные веса слоёв нейросети для сохранения в JSON на диск."""
        arch_str = (
            f"Input({self.input_dim}) -> Dense({self.h1}, LeakyReLU) -> "
            f"Dense({self.h2}, LeakyReLU) -> Dense(3, Sigmoid)"
        )
        return {
            "architecture": arch_str,
            "backend": self.backend_used,
            "input_dim": self.input_dim,
            "hidden_dims": [self.h1, self.h2],
            "total_parameters": self.total_parameters,
            "learning_rate": self.lr,
            "weight_decay": self.weight_decay,
            "epochs": len(self.train_loss_curve),
            "train_loss_curve": [round(v, 5) for v in self.train_loss_curve],
            "val_loss_curve": [round(v, 5) for v in self.val_loss_curve],
            "feature_means": [round(v, 6) for v in self.feature_means],
            "feature_stds": [round(v, 6) for v in self.feature_stds],
            "W1": [[round(v, 5) for v in row] for row in self.w1],
            "b1": [round(v, 5) for v in self.b1],
            "W2": [[round(v, 5) for v in row] for row in self.w2],
            "b2": [round(v, 5) for v in self.b2],
            "W3": [[round(v, 5) for v in row] for row in self.w3],
            "b3": [round(v, 5) for v in self.b3],
        }

    def load_weights_dict(self, data: dict[str, Any]) -> bool:
        """Загружает сохранённые веса нейросети с диска без повторного обучения."""
        try:
            self.input_dim = int(data["input_dim"])
            h_dims = data.get("hidden_dims", [self.h1, self.h2])
            self.h1 = int(h_dims[0])
            self.h2 = int(h_dims[1])
            self.feature_means = [float(x) for x in data["feature_means"]]
            self.feature_stds = [float(x) for x in data["feature_stds"]]
            self.w1 = [[float(x) for x in row] for row in data["W1"]]
            self.b1 = [float(x) for x in data["b1"]]
            self.w2 = [[float(x) for x in row] for row in data["W2"]]
            self.b2 = [float(x) for x in data["b2"]]
            self.w3 = [[float(x) for x in row] for row in data["W3"]]
            self.b3 = [float(x) for x in data["b3"]]
            self.train_loss_curve = [float(x) for x in data.get("train_loss_curve", [0.693])]
            self.val_loss_curve = [float(x) for x in data.get("val_loss_curve", [0.693])]
            self.backend_used = str(data.get("backend", "loaded_from_disk"))
            return self.input_dim > 0 and len(self.w1) == self.input_dim
        except (KeyError, ValueError, TypeError, IndexError):
            return False
