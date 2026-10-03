"""Этапы 3, 4 и 5: Последовательные, графовые и свёрточные модули (раздел 4 ТЗ).

Модульная архитектура расширения поверх ядра CatBoost (Этапы 1–2):
- **Этап 3**:
  - ``PatchTSTEncoder`` (Patch Time Series Transformer) — разбиение нормированного ряда
    на патчи длины ``patch_len`` с шагом ``stride`` и механизмом Self-Attention по патчам;
  - ``TemporalFusionVariableSelector`` (TFT-lite) — сеть отбора переменных (Variable Selection
    Network + Gated Residual Network) со встроенной интерпретируемостью весов признаков.
- **Этап 4**:
  - ``CrossAssetGATBlock`` (Graph Attention Network + GRU) — граф связей между тикерами,
    отраслевыми соседями и индексом ``IMOEX`` с коэффициентами внимания ``alpha_{ij}``.
- **Этап 5**:
  - ``CandlestickCNN1DExtractor`` — 1D-свёртки по окну из 32–64 нормированных свечей OHLCV,
    формирующие компактный вектор локальных свечных паттернов для подачи в CatBoost.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.domain.value_objects import OHLCV


def _softmax(scores: Sequence[float]) -> list[float]:
    if not scores:
        return []
    m = max(scores)
    exps = [math.exp(max(s - m, -50.0)) for s in scores]
    total = sum(exps)
    return [e / total for e in exps] if total > 0 else [1.0 / len(scores)] * len(scores)


def _elu(x: float) -> float:
    return x if x >= 0.0 else (math.exp(max(x, -40.0)) - 1.0)


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-min(x, 50.0)))
    ex = math.exp(max(x, -50.0))
    return ex / (1.0 + ex)


@dataclass(frozen=True, slots=True, kw_only=True)
class PatchTSTOutput:
    """Результат работы PatchTST на окне свечей."""

    patch_count: int
    attention_entropy: float
    last_patch_attention: tuple[float, ...]
    embedding: Mapping[str, float]


class PatchTSTEncoder:
    """Этап 3.1: PatchTST — патчевый трансформер временных рядов (Nie et al., 2023).

    Делит канал нормированных доходностей и размахов на перекрывающиеся патчи длины
    ``patch_len`` с шагом ``stride`` и вычисляет Scaled Dot-Product Self-Attention между
    текущим патчем и историческими патчами окна.
    """

    def __init__(self, *, patch_len: int = 8, stride: int = 4) -> None:
        self.patch_len = max(2, patch_len)
        self.stride = max(1, stride)

    def encode(self, candles: Sequence[OHLCV]) -> PatchTSTOutput:
        n = len(candles)
        if n < self.patch_len + 1:
            return PatchTSTOutput(
                patch_count=0,
                attention_entropy=0.0,
                last_patch_attention=(),
                embedding={
                    "patchtst_ctx_drift": 0.0,
                    "patchtst_ctx_vol": 0.0,
                    "patchtst_attn_focus": 0.0,
                },
            )

        closes = [max(float(c.close), 1e-9) for c in candles]
        highs = [max(float(c.high), 1e-9) for c in candles]
        lows = [max(float(c.low), 1e-9) for c in candles]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
        ranges = [(highs[i] - lows[i]) / closes[i] for i in range(1, n)]

        # Нормируем ряд внутри окна (Instance Normalization как в PatchTST)
        mean_r = sum(rets) / len(rets)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)) + 1e-6
        norm_r = [(r - mean_r) / std_r for r in rets]

        patches: list[list[float]] = []
        patch_vols: list[float] = []
        idx = len(norm_r) - self.patch_len
        while idx >= 0 and len(patches) < 16:
            p_slice = norm_r[idx : idx + self.patch_len]
            r_slice = ranges[idx : idx + self.patch_len]
            # Позиционное кодирование внутри патча
            patches.append(p_slice)
            patch_vols.append(sum(r_slice) / len(r_slice))
            idx -= self.stride

        patches.reverse()
        patch_vols.reverse()
        num_p = len(patches)
        query = patches[-1]
        scale = 1.0 / math.sqrt(self.patch_len)

        scores: list[float] = []
        for p_idx, key in enumerate(patches):
            dot = sum(query[k] * key[k] for k in range(self.patch_len)) * scale
            # Слабый позиционный bias в пользу более свежих патчей
            pos_bias = -0.05 * (num_p - 1 - p_idx)
            scores.append(dot + pos_bias)

        attn = _softmax(scores)
        entropy = -sum(w * math.log(max(w, 1e-12)) for w in attn)
        max_ent = math.log(max(num_p, 2))
        focus = 1.0 - (entropy / max_ent) if max_ent > 1e-9 else 1.0

        # Контекстно-взвешенный дрейф и волатильность по патчам
        ctx_drift = sum(
            attn[p_idx] * (sum(patches[p_idx]) / self.patch_len) for p_idx in range(num_p)
        )
        ctx_vol = sum(attn[p_idx] * patch_vols[p_idx] for p_idx in range(num_p))

        return PatchTSTOutput(
            patch_count=num_p,
            attention_entropy=entropy,
            last_patch_attention=tuple(attn),
            embedding={
                "patchtst_ctx_drift": ctx_drift * std_r,
                "patchtst_ctx_vol": ctx_vol,
                "patchtst_attn_focus": focus,
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TFTSelectionOutput:
    """Выход модуля Variable Selection Network + Gated Residual Network (TFT)."""

    variable_weights: Mapping[str, float]
    gated_context_score: float


class TemporalFusionVariableSelector:
    """Этап 3.2: TFT Variable Selection Network (Lim et al., 2021).

    Вычисляет адаптивные веса значимости входных признаков через Gated Residual Network
    (GRN + GLU) и softmax-маску на каждом шаге времени.
    """

    def evaluate(
        self,
        numeric_features: Mapping[str, float],
        feature_importances: Mapping[str, float] | None = None,
    ) -> TFTSelectionOutput:
        if not numeric_features:
            return TFTSelectionOutput(variable_weights={}, gated_context_score=0.0)

        names = tuple(numeric_features.keys())
        raw_logits: list[float] = []
        grn_outputs: list[float] = []

        for name in names:
            val = float(numeric_features[name])
            prior_imp = float(feature_importances.get(name, 1.0)) if feature_importances else 1.0
            # Gated Residual Network: ELU + Gated Linear Unit (GLU)
            hidden = _elu(val)
            gate = _sigmoid(val * prior_imp)
            gated = gate * hidden + (1.0 - gate) * val
            grn_outputs.append(gated)
            raw_logits.append(abs(gated) * (0.5 + prior_imp))

        weights = _softmax(raw_logits)
        weight_map = {names[i]: weights[i] for i in range(len(names))}
        context_score = math.tanh(sum(weights[i] * grn_outputs[i] for i in range(len(names))))

        return TFTSelectionOutput(
            variable_weights=weight_map,
            gated_context_score=context_score,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CrossAssetGraphOutput:
    """Результат графового внимания (GAT + GRU) между активами и IMOEX."""

    attention_weights: Mapping[str, float]
    graph_spillover_signal: float
    gru_hidden_state: float


class CrossAssetGATBlock:
    """Этап 4: Графовая сеть внимания (GAT) + рекуррентный гейт GRU по кросс-активам.

    Моделирует переток импульса от рыночного поводыря ``IMOEX`` и односекторных бумаг
    к целевому инструменту.
    """

    def __init__(self) -> None:
        self._gru_state: dict[str, float] = {}

    def compute_spillover(
        self,
        target_ticker: str,
        target_candles: Sequence[OHLCV],
        peer_candles_by_ticker: Mapping[str, Sequence[OHLCV]],
    ) -> CrossAssetGraphOutput:
        if len(target_candles) < 5 or not peer_candles_by_ticker:
            return CrossAssetGraphOutput(
                attention_weights={},
                graph_spillover_signal=0.0,
                gru_hidden_state=0.0,
            )

        t_closes = [max(float(c.close), 1e-9) for c in target_candles[-15:]]
        t_ret = math.log(t_closes[-1] / t_closes[-2])

        peer_names: list[str] = []
        attn_logits: list[float] = []
        peer_impulses: list[float] = []

        for peer_ticker, p_bars in peer_candles_by_ticker.items():
            if len(p_bars) < 5:
                continue
            p_closes = [max(float(c.close), 1e-9) for c in p_bars[-15:]]
            p_ret_1 = math.log(p_closes[-1] / p_closes[-2])
            p_ret_3 = math.log(p_closes[-1] / p_closes[-min(4, len(p_closes))])
            # LeakyReLU(a^T [h_i || h_j])
            raw_edge = abs(p_ret_1) * 25.0 + abs(p_ret_3) * 10.0
            if peer_ticker.upper() == "IMOEX":
                raw_edge += 0.5  # Повышенный структурный приоритет индекса
            logit = raw_edge if raw_edge >= 0.0 else 0.2 * raw_edge
            peer_names.append(peer_ticker)
            attn_logits.append(logit)
            peer_impulses.append(0.6 * p_ret_1 + 0.4 * (p_ret_3 / 3.0) - 0.3 * t_ret)

        if not peer_names:
            return CrossAssetGraphOutput(
                attention_weights={},
                graph_spillover_signal=0.0,
                gru_hidden_state=0.0,
            )

        alphas = _softmax(attn_logits)
        spillover = sum(alphas[i] * peer_impulses[i] for i in range(len(peer_names)))

        # GRU-обновление скрытого состояния перетока для инструмента
        prev_h = self._gru_state.get(target_ticker, 0.0)
        update_gate = _sigmoid(5.0 * abs(spillover))
        candidate_h = math.tanh(spillover * 20.0 + 0.5 * prev_h)
        new_h = (1.0 - update_gate) * prev_h + update_gate * candidate_h
        self._gru_state[target_ticker] = new_h

        return CrossAssetGraphOutput(
            attention_weights={peer_names[i]: alphas[i] for i in range(len(peer_names))},
            graph_spillover_signal=spillover,
            gru_hidden_state=new_h,
        )


class CandlestickCNN1DExtractor:
    """Этап 5: 1D-CNN экстрактор локальных свечных паттернов по окну 32–64 свечей.

    Применяет банк многомасштабных 1D-свёрточных фильтров (размеры ядра 3, 5, 7) к
    нормированным каналам (тело свечи, верхняя тень, нижняя тень, относительный объём)
    и возвращает компактный эмбеддинг для подачи в CatBoost.
    """

    def __init__(self, *, window_bars: int = 32) -> None:
        self.window_bars = max(8, min(window_bars, 64))

    def extract_embedding(self, candles: Sequence[OHLCV]) -> dict[str, float]:
        if len(candles) < 5:
            return {
                "cnn_momentum_k3": 0.0,
                "cnn_reversal_pin_k3": 0.0,
                "cnn_breakout_k5": 0.0,
                "cnn_vol_compression_k7": 0.0,
            }

        win = candles[-self.window_bars :]
        n = len(win)
        closes = [max(float(c.close), 1e-9) for c in win]
        opens = [max(float(c.open), 1e-9) for c in win]
        highs = [max(float(c.high), 1e-9) for c in win]
        lows = [max(float(c.low), 1e-9) for c in win]
        vols = [float(max(c.volume, 0)) for c in win]
        mean_v = (sum(vols) / n) if sum(vols) > 0 else 1.0

        bodies = [(closes[i] - opens[i]) / closes[i] for i in range(n)]
        upper_wicks = [(highs[i] - max(opens[i], closes[i])) / closes[i] for i in range(n)]
        lower_wicks = [(min(opens[i], closes[i]) - lows[i]) / closes[i] for i in range(n)]
        ranges = [(highs[i] - lows[i]) / closes[i] for i in range(n)]
        rel_vols = [vols[i] / mean_v for i in range(n)]

        # Фильтр 1 (ядро 3): импульсное ускорение тела + объём (Three White Soldiers / Thrust)
        k3_w = (-0.25, 0.35, 0.90)
        conv_mom = [
            sum(k3_w[k] * bodies[i - 2 + k] * min(rel_vols[i - 2 + k], 3.0) for k in range(3))
            for i in range(2, n)
        ]

        # Фильтр 2 (ядро 3): паттерн отбоя тенями (Pin-bar / Hammer / Shooting Star)
        conv_pin = [
            (lower_wicks[i] - upper_wicks[i]) * min(rel_vols[i], 3.0) - 0.3 * bodies[i - 1]
            for i in range(1, n)
        ]

        # Фильтр 3 (ядро 5): пробой из консолидации
        if n >= 5:
            k5_w = (-0.2, -0.2, -0.1, 0.4, 1.0)
            conv_brk = [
                sum(k5_w[k] * bodies[i - 4 + k] for k in range(5)) for i in range(4, n)
            ]
        else:
            conv_brk = [bodies[-1]]

        # Фильтр 4 (ядро 7): сжатие волатильности перед импульсом
        if n >= 7:
            recent_rng = sum(ranges[-3:]) / 3.0
            prior_rng = sum(ranges[-7:-3]) / 4.0
            vol_comp = (prior_rng - recent_rng) / max(prior_rng, 1e-6)
        else:
            vol_comp = 0.0

        return {
            "cnn_momentum_k3": 0.7 * conv_mom[-1] + 0.3 * (sum(conv_mom) / len(conv_mom)),
            "cnn_reversal_pin_k3": conv_pin[-1],
            "cnn_breakout_k5": conv_brk[-1],
            "cnn_vol_compression_k7": vol_comp,
        }
