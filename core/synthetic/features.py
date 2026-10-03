"""Генерация признаков «Синтетического трейдера» (Слой A + Слой B + IMOEX, раздел 3 ТЗ).

Ключевые инварианты:
- Запрещена подача «сырых» уровней цен или скользящих средних: все 13 опорных MA/EMA
  (SMA 5/10/20/50/100/200 и EMA 8/13/21/34/55/89/144) кодируются строго как
  **относительное расстояние** ``(close - MA) / close`` и **нормированный наклон**
  ``(MA_t - MA_{t-3}) / (3 * close)``.
- Старшие таймфреймы (1D, 4H) подтягиваются только из полностью закрытых баров
  через ``AlignedMultiTimeframeSnapshot``.
- Поддерживаются категориальные признаки (``ticker``, ``class_code``, ``day_of_week``)
  для нативной обработки в CatBoost.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.domain.value_objects import OHLCV
from core.synthetic.feature_store import AlignedMultiTimeframeSnapshot

#: Опорные периоды SMA и EMA (всего 13 скользящих средних — строго в диапазоне 5–15 по ТЗ).
REFERENCE_SMA_PERIODS: tuple[int, ...] = (5, 10, 20, 50, 100, 200)
REFERENCE_EMA_PERIODS: tuple[int, ...] = (8, 13, 21, 34, 55, 89, 144)

#: Официальные нерабочие праздничные даты РФ (месяц, день) для признака предпраздничного дня.
RU_HOLIDAY_MONTH_DAYS: frozenset[tuple[int, int]] = frozenset(
    {
        (1, 1),
        (1, 2),
        (1, 7),
        (2, 23),
        (3, 8),
        (5, 1),
        (5, 9),
        (6, 12),
        (11, 4),
        (12, 31),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticFeatureVector:
    """Точка признакового пространства на момент закрытия бара ``timestamp``."""

    timestamp: datetime
    instrument_uid: str
    numeric_features: Mapping[str, float]
    categorical_features: Mapping[str, str]

    def to_decimal_map(self) -> dict[str, Decimal]:
        """Представление числовых признаков в ``Decimal`` для доменного аудита и БД."""
        return {k: Decimal(f"{v:.8f}") for k, v in self.numeric_features.items()}


def _ema_tail(values: Sequence[float], period: int, *, lookback_multiplier: int = 3) -> list[float]:
    """Вычисляет хвост EMA длины до ``len(values)``."""
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    start_idx = max(0, len(values) - period * lookback_multiplier)
    sub = values[start_idx:]
    ema_val = sub[0]
    out = [ema_val]
    for v in sub[1:]:
        ema_val = alpha * v + (1.0 - alpha) * ema_val
        out.append(ema_val)
    return out


def _rsi_value(closes: Sequence[float], period: int) -> float:
    """Вычисляет RSI Уайлдера в диапазоне ``[0, 100]``, нормированный к ``[-1, +1]``."""
    if len(closes) < period + 1:
        return 0.0
    window = closes[-(period * 2 + 1) :]
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(window)):
        diff = window[i] - window[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 1e-12:
        rsi = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    return (rsi - 50.0) / 50.0


def _is_pre_holiday_or_weekend(ts: datetime) -> float:
    """Признак пятницы / предвыходного или предпраздничного торгового дня MOEX."""
    weekday = ts.weekday()
    if weekday >= 4:
        return 1.0
    # Проверяем следующий календарный день
    next_day = ts.ordinal() + 1 if hasattr(ts, "ordinal") else ts.toordinal() + 1
    next_dt = datetime.fromordinal(next_day)
    if (next_dt.month, next_dt.day) in RU_HOLIDAY_MONTH_DAYS:
        return 1.0
    return 0.0


def extract_layer_a_features(candles: Sequence[OHLCV]) -> dict[str, float]:
    """Слой A (базовый): лог-доходности, волатильность, объём, 13 MA/EMA и календарь."""
    if not candles:
        return {}

    closes = [max(float(c.close), 1e-9) for c in candles]
    highs = [max(float(c.high), 1e-9) for c in candles]
    lows = [max(float(c.low), 1e-9) for c in candles]
    opens = [max(float(c.open), 1e-9) for c in candles]
    volumes = [float(max(c.volume, 0)) for c in candles]
    n = len(closes)
    cur_close = closes[-1]
    last_bar = candles[-1]

    features: dict[str, float] = {}

    # 1. Лог-доходности на нескольких лагах
    for lag in (1, 2, 3, 5, 10, 20):
        if n > lag:
            features[f"log_ret_{lag}"] = math.log(cur_close / closes[-1 - lag])
        else:
            features[f"log_ret_{lag}"] = 0.0

    # 2. Размах свечи, тело свечи, True Range, ATR(14) (нормированные к цене)
    cur_range = (highs[-1] - lows[-1]) / cur_close
    cur_body = (closes[-1] - opens[-1]) / cur_close
    prev_close = closes[-2] if n >= 2 else cur_close
    cur_tr = max(
        highs[-1] - lows[-1],
        abs(highs[-1] - prev_close),
        abs(lows[-1] - prev_close),
    )
    features["candle_range_pct"] = cur_range
    features["candle_body_pct"] = cur_body
    features["true_range_pct"] = cur_tr / cur_close

    tr_window: list[float] = []
    start_tr = max(1, n - 28)
    for i in range(start_tr, n):
        tr_i = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_window.append(tr_i)
    atr_14 = (sum(tr_window[-14:]) / len(tr_window[-14:])) if tr_window else cur_tr
    features["atr_14_pct"] = atr_14 / cur_close

    # 3. Реализованная волатильность на окнах 5, 20, 60 баров
    log_rets: list[float] = [
        math.log(closes[i] / closes[i - 1]) for i in range(max(1, n - 65), n)
    ]
    for win in (5, 20, 60):
        sub_r = log_rets[-win:]
        if len(sub_r) >= 2:
            mean_r = sum(sub_r) / len(sub_r)
            var_r = sum((r - mean_r) ** 2 for r in sub_r) / (len(sub_r) - 1)
            features[f"realized_vol_{win}"] = math.sqrt(var_r)
        else:
            features[f"realized_vol_{win}"] = 0.0

    vol_20 = features["realized_vol_20"]
    features["vol_ratio_5_20"] = (
        (features["realized_vol_5"] / vol_20 - 1.0) if vol_20 > 1e-9 else 0.0
    )

    # 4. Объём относительно своего среднего, z-score и знаковая дельта объёма
    vol_win = volumes[-20:]
    mean_vol = sum(vol_win) / len(vol_win) if vol_win else 1.0
    std_vol = (
        math.sqrt(sum((v - mean_vol) ** 2 for v in vol_win) / max(len(vol_win) - 1, 1))
        if len(vol_win) >= 2
        else 1.0
    )
    features["volume_rel_20"] = (volumes[-1] / mean_vol - 1.0) if mean_vol > 1e-9 else 0.0
    features["volume_zscore_20"] = (
        (volumes[-1] - mean_vol) / std_vol if std_vol > 1e-9 else 0.0
    )

    signed_vols: list[float] = []
    for i in range(max(0, n - 10), n):
        rng = highs[i] - lows[i]
        clv = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng if rng > 1e-9 else 0.0
        signed_vols.append(clv * volumes[i])
    tot_vol_10 = sum(volumes[-10:])
    features["volume_signed_delta_10"] = (
        sum(signed_vols) / tot_vol_10 if tot_vol_10 > 1e-9 else 0.0
    )

    # 5. Относительное расстояние (%) и наклон к 13 опорным MA/EMA (6 SMA + 7 EMA)
    for p in REFERENCE_SMA_PERIODS:
        w = closes[-p:]
        ma_now = sum(w) / len(w)
        features[f"sma_{p}_dist_pct"] = (cur_close - ma_now) / cur_close
        if n > p + 3:
            w_prev = closes[-p - 3 : -3]
            ma_prev = sum(w_prev) / len(w_prev)
            features[f"sma_{p}_slope_pct"] = (ma_now - ma_prev) / (3.0 * cur_close)
        else:
            features[f"sma_{p}_slope_pct"] = 0.0

    for p in REFERENCE_EMA_PERIODS:
        ema_series = _ema_tail(closes, p)
        ema_now = ema_series[-1]
        features[f"ema_{p}_dist_pct"] = (cur_close - ema_now) / cur_close
        if len(ema_series) >= 4:
            ema_prev = ema_series[-4]
            features[f"ema_{p}_slope_pct"] = (ema_now - ema_prev) / (3.0 * cur_close)
        else:
            features[f"ema_{p}_slope_pct"] = 0.0

    # 6. Календарные признаки (тригонометрическое представление + флаг предпраздничного дня)
    hour = last_bar.timestamp.hour + last_bar.timestamp.minute / 60.0
    dow = float(last_bar.timestamp.weekday())
    features["calendar_hour_sin"] = math.sin(2.0 * math.pi * hour / 24.0)
    features["calendar_hour_cos"] = math.cos(2.0 * math.pi * hour / 24.0)
    features["calendar_dow_num"] = dow / 4.0
    features["calendar_pre_holiday"] = _is_pre_holiday_or_weekend(last_bar.timestamp)

    return features


def extract_layer_b_features(
    candles: Sequence[OHLCV],
    *,
    imoex_candles: Sequence[OHLCV] = (),
    snapshot: AlignedMultiTimeframeSnapshot | None = None,
) -> dict[str, float]:
    """Слой B (расширенный): осцилляторы, уровни, мультитаймфрейм и кросс-актив к IMOEX."""
    if not candles:
        return {}

    closes = [max(float(c.close), 1e-9) for c in candles]
    highs = [max(float(c.high), 1e-9) for c in candles]
    lows = [max(float(c.low), 1e-9) for c in candles]
    volumes = [float(max(c.volume, 0)) for c in candles]
    n = len(closes)
    cur_close = closes[-1]

    features: dict[str, float] = {}

    # 1. RSI на 3 периодах (7, 14, 21)
    features["rsi_7_norm"] = _rsi_value(closes, 7)
    features["rsi_14_norm"] = _rsi_value(closes, 14)
    features["rsi_21_norm"] = _rsi_value(closes, 21)

    # 2. MACD (12, 26, 9), нормированный к текущей цене
    ema12 = _ema_tail(closes, 12)
    ema26 = _ema_tail(closes, 26)
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-min_len + i] - ema26[-min_len + i] for i in range(min_len)]
    signal_line = _ema_tail(macd_line, 9) if macd_line else [0.0]
    macd_now = macd_line[-1] / cur_close if macd_line else 0.0
    sig_now = signal_line[-1] / cur_close if signal_line else 0.0
    features["macd_norm"] = macd_now
    features["macd_signal_norm"] = sig_now
    features["macd_hist_norm"] = macd_now - sig_now

    # 3. Stochastic (14, 3)
    h14 = max(highs[-14:])
    l14 = min(lows[-14:])
    stoch_k = ((cur_close - l14) / (h14 - l14)) if (h14 - l14) > 1e-9 else 0.5
    stoch_k_hist: list[float] = []
    for k_back in range(min(3, n)):
        idx_end = n - k_back
        idx_start = max(0, idx_end - 14)
        hh = max(highs[idx_start:idx_end])
        ll = min(lows[idx_start:idx_end])
        sk = ((closes[idx_end - 1] - ll) / (hh - ll)) if (hh - ll) > 1e-9 else 0.5
        stoch_k_hist.append(sk)
    stoch_d = sum(stoch_k_hist) / len(stoch_k_hist)
    features["stoch_k_14"] = stoch_k - 0.5
    features["stoch_d_14"] = stoch_d - 0.5

    # 4. Bollinger Band width и %B (20, 2.0)
    w20 = closes[-20:]
    ma20 = sum(w20) / len(w20)
    std20 = (
        math.sqrt(sum((c - ma20) ** 2 for c in w20) / max(len(w20) - 1, 1))
        if len(w20) >= 2
        else 0.0
    )
    upper_bb = ma20 + 2.0 * std20
    lower_bb = ma20 - 2.0 * std20
    features["bb_width_20"] = (upper_bb - lower_bb) / ma20 if ma20 > 1e-9 else 0.0
    features["bb_percent_b_20"] = (
        (cur_close - lower_bb) / (upper_bb - lower_bb)
        if (upper_bb - lower_bb) > 1e-9
        else 0.5
    )

    # 5. OBV z-score и отклонение от окна VWAP
    obv = 0.0
    obv_series: list[float] = [0.0]
    start_obv = max(1, n - 25)
    for i in range(start_obv, n):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        obv_series.append(obv)
    obv_mean = sum(obv_series) / len(obv_series)
    obv_std = math.sqrt(
        sum((v - obv_mean) ** 2 for v in obv_series) / max(len(obv_series) - 1, 1)
    )
    features["obv_zscore_20"] = (
        (obv_series[-1] - obv_mean) / obv_std if obv_std > 1e-9 else 0.0
    )

    vwap_num = 0.0
    vwap_den = 0.0
    for i in range(max(0, n - 20), n):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        vwap_num += typical * volumes[i]
        vwap_den += volumes[i]
    vwap_val = vwap_num / vwap_den if vwap_den > 1e-9 else cur_close
    features["vwap_dev_pct"] = (cur_close - vwap_val) / cur_close

    # 6. Расстояние до локальных/исторических максимумов и минимумов, а также круглых уровней
    for win in (20, 60):
        hi_w = max(highs[-win:])
        lo_w = min(lows[-win:])
        features[f"dist_high_{win}_pct"] = (cur_close - hi_w) / cur_close
        features[f"dist_low_{win}_pct"] = (cur_close - lo_w) / cur_close

    # Ближайший круглый уровень шага 10^(floor(log10(price)) - 1)
    magnitude = 10.0 ** max(math.floor(math.log10(cur_close)) - 1, 0)
    nearest_round = round(cur_close / magnitude) * magnitude
    features["dist_round_level_pct"] = (cur_close - nearest_round) / cur_close

    # 7. Кросс-активные признаки относительно индекса МосБиржи (IMOEX)
    if len(imoex_candles) >= 2 and n >= 2:
        im_closes = [max(float(c.close), 1e-9) for c in imoex_candles]
        im_n = len(im_closes)
        asset_ret_1 = math.log(closes[-1] / closes[-2])
        im_ret_1 = math.log(im_closes[-1] / im_closes[-2])
        features["imoex_ret_spread_1"] = asset_ret_1 - im_ret_1

        if n > 5 and im_n > 5:
            asset_ret_5 = math.log(closes[-1] / closes[-6])
            im_ret_5 = math.log(im_closes[-1] / im_closes[-6])
            features["imoex_ret_spread_5"] = asset_ret_5 - im_ret_5
        else:
            features["imoex_ret_spread_5"] = 0.0

        # Скользящая корреляция и бета на окне 20
        win_corr = min(20, n - 1, im_n - 1)
        if win_corr >= 5:
            a_rets = [
                math.log(closes[-win_corr - 1 + k + 1] / closes[-win_corr - 1 + k])
                for k in range(win_corr)
            ]
            b_rets = [
                math.log(im_closes[-win_corr - 1 + k + 1] / im_closes[-win_corr - 1 + k])
                for k in range(win_corr)
            ]
            ma_a = sum(a_rets) / win_corr
            ma_b = sum(b_rets) / win_corr
            cov = sum((a_rets[k] - ma_a) * (b_rets[k] - ma_b) for k in range(win_corr))
            var_a = sum((a_rets[k] - ma_a) ** 2 for k in range(win_corr))
            var_b = sum((b_rets[k] - ma_b) ** 2 for k in range(win_corr))
            denom = math.sqrt(var_a * var_b)
            features["imoex_corr_20"] = cov / denom if denom > 1e-12 else 0.0
            features["imoex_beta_20"] = cov / var_b if var_b > 1e-12 else 1.0
        else:
            features["imoex_corr_20"] = 0.0
            features["imoex_beta_20"] = 1.0

        # Наклон линии относительной силы (RS-line)
        rs_lookback = min(10, n - 1, im_n - 1)
        rs_now = closes[-1] / im_closes[-1]
        rs_prev = closes[-1 - rs_lookback] / im_closes[-1 - rs_lookback]
        features["imoex_rs_slope_10"] = (rs_now - rs_prev) / max(rs_prev, 1e-9)
    else:
        features["imoex_ret_spread_1"] = 0.0
        features["imoex_ret_spread_5"] = 0.0
        features["imoex_corr_20"] = 0.0
        features["imoex_beta_20"] = 1.0
        features["imoex_rs_slope_10"] = 0.0

    # 8. Признаки старших таймфреймов (только по полностью закрытым барам из snapshot)
    if snapshot is not None:
        for htf_key in ("1d", "4h"):
            htf_bars = snapshot.closed_bars_by_tf.get(htf_key, ())
            if len(htf_bars) >= 2:
                hc = [max(float(b.close), 1e-9) for b in htf_bars]
                features[f"htf_{htf_key}_ret_1"] = math.log(hc[-1] / hc[-2])
                ema21_htf = _ema_tail(hc, 21)[-1]
                features[f"htf_{htf_key}_ema21_dist"] = (hc[-1] - ema21_htf) / hc[-1]
            else:
                features[f"htf_{htf_key}_ret_1"] = 0.0
                features[f"htf_{htf_key}_ema21_dist"] = 0.0
    else:
        features["htf_1d_ret_1"] = 0.0
        features["htf_1d_ema21_dist"] = 0.0
        features["htf_4h_ret_1"] = 0.0
        features["htf_4h_ema21_dist"] = 0.0

    return features


def build_feature_vector(
    candles: Sequence[OHLCV],
    *,
    instrument_uid: str = "UNKNOWN",
    ticker: str = "UNKNOWN",
    class_code: str = "TQBR",
    include_layer_b: bool = True,
    imoex_candles: Sequence[OHLCV] = (),
    snapshot: AlignedMultiTimeframeSnapshot | None = None,
) -> SyntheticFeatureVector:
    """Собирает полный вектор признаков (Слой A + опционально Слой B + категориальные)."""
    if not candles:
        msg = "Для расчёта признаков необходима хотя бы одна закрытая свеча"
        raise ValueError(msg)

    numeric = extract_layer_a_features(candles)
    if include_layer_b:
        numeric.update(
            extract_layer_b_features(
                candles,
                imoex_candles=imoex_candles,
                snapshot=snapshot,
            )
        )

    last_ts = candles[-1].timestamp
    categorical = {
        "ticker": ticker,
        "class_code": class_code,
        "day_of_week": str(last_ts.weekday()),
    }
    return SyntheticFeatureVector(
        timestamp=last_ts,
        instrument_uid=instrument_uid,
        numeric_features=numeric,
        categorical_features=categorical,
    )
