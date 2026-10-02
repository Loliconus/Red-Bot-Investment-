"""Point-in-time фичестор: asof-join исключительно по availability закрытого бара."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.data import MarketDataset, aggregate_closed_bars


@dataclass(slots=True)
class FeatureSet:
    rows: pd.DataFrame
    columns: list[str]
    provenance: dict[str, str]
    warnings: list[str]


def point_in_time_join(base: pd.DataFrame, higher: pd.DataFrame, *, prefix: str) -> pd.DataFrame:
    """Ни bfill, ни join по началу бара. Возвращает audit availability каждого join."""
    if "available_at" not in higher:
        raise ValueError("Higher-frame features требуют available_at")
    base = base.copy()
    source = higher.sort_values("available_at").copy()
    if source["available_at"].duplicated().any():
        raise ValueError("Неоднозначные версии признаков на одном availability timestamp")
    if source["available_at"].dt.tz is None or base["asof"].dt.tz is None:
        raise ValueError("PIT join требует aware timestamps")
    # Pandas 3 / DuckDB: Etc/UTC vs UTC и us vs ns логически равны, но
    # merge_asof требует одинаковый dtype. Канонизируем без потери точности.
    base["asof"] = base["asof"].dt.tz_convert("UTC").dt.as_unit("ns")
    source["available_at"] = source["available_at"].dt.tz_convert("UTC").dt.as_unit("ns")
    source = source.rename(columns={c: f"{prefix}{c}" for c in source if c != "available_at"})
    key = f"audit_{prefix}available_at"
    source = source.rename(columns={"available_at": key})
    result = pd.merge_asof(
        base.sort_values("asof"),
        source,
        left_on="asof",
        right_on=key,
        direction="backward",
        allow_exact_matches=True,
    )
    if (result[key].dropna() > result.loc[result[key].notna(), "asof"]).any():
        raise ValueError("Обнаружена утечка higher-frame availability")
    return result


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    denominator = gains + losses
    return (100 * gains / denominator.replace(0, np.nan)).fillna(50.0).where(denominator.notna())


def bar_features(frame: pd.DataFrame, *, atr_period: int, layer_b: bool) -> pd.DataFrame:
    data = frame.sort_values("end").copy()
    close, high, low, volume = (data[c].astype(float) for c in ("close", "high", "low", "volume"))
    log_close = np.log(close)
    returns = log_close.diff()
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
    cols: dict[str, Any] = {
        "available_at": data["end"],
        "atr_pct": atr / close,
        "range_pct": (high - low) / close,
        "body_pct": (close - data["open"].astype(float)) / close,
        "close_location": (close - low) / (high - low).replace(0, np.nan),
        "atr": atr,
    }
    for lag in (1, 2, 5, 10, 20):
        cols[f"return_{lag}"] = log_close.diff(lag)
    for window in (10, 20, 60):
        cols[f"rv_{window}"] = returns.rolling(window, min_periods=window).std()
        log_volume = np.log1p(volume)
        cols[f"volume_z_{window}"] = (
            log_volume - log_volume.rolling(window).mean()
        ) / log_volume.rolling(window).std().replace(0, np.nan)
    cols["volume_delta"] = np.log1p(volume).diff()
    for window in (5, 10, 20, 40, 80, 160):
        ema = close.ewm(span=window, adjust=False, min_periods=window).mean()
        cols[f"ema_dist_{window}"] = close / ema - 1
        cols[f"ema_slope_{window}"] = ema.pct_change(5)
    local = data["end"].dt.tz_convert("Europe/Moscow")
    hour = local.dt.hour + local.dt.minute / 60
    cols["hour_sin"], cols["hour_cos"] = (
        np.sin(2 * np.pi * hour / 24),
        np.cos(2 * np.pi * hour / 24),
    )
    cols["weekday_sin"], cols["weekday_cos"] = (
        np.sin(2 * np.pi * local.dt.dayofweek / 7),
        np.cos(2 * np.pi * local.dt.dayofweek / 7),
    )
    # Не выдумываем исторический календарь MOEX: pre-holiday только при внешнем calendar snapshot.
    if layer_b:
        for period in (7, 14, 21):
            cols[f"rsi_{period}"] = rsi(close, period) / 100
            rolling_high, rolling_low = high.rolling(period).max(), low.rolling(period).min()
            cols[f"stochastic_{period}"] = (close - rolling_low) / (
                rolling_high - rolling_low
            ).replace(0, np.nan)
        ema12, ema26 = (
            close.ewm(span=12, adjust=False).mean(),
            close.ewm(span=26, adjust=False).mean(),
        )
        macd = (ema12 - ema26) / close
        cols["macd"] = macd
        cols["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
        for window in (20, 60):
            cols[f"bollinger_width_{window}"] = (
                4 * close.rolling(window).std() / close.rolling(window).mean()
            )
            cols[f"distance_high_{window}"] = close / high.rolling(window).max() - 1
            cols[f"distance_low_{window}"] = close / low.rolling(window).min() - 1
        signed_volume = np.sign(returns).fillna(0) * volume
        cols["obv_delta_20"] = signed_volume.rolling(20).sum() / volume.rolling(20).sum().replace(
            0, np.nan
        )
        day = local.dt.normalize()
        typical = (high + low + close) / 3
        vwap = (typical * volume).groupby(day).cumsum() / volume.groupby(day).cumsum().replace(
            0, np.nan
        )
        cols["vwap_distance"] = close / vwap - 1
        # Реперный round level выбирается только по текущей цене.
        magnitude = np.power(10.0, np.floor(np.log10(close)) - 1)
        cols["round_level_distance"] = (
            (close / magnitude - np.round(close / magnitude)) * magnitude / close
        )
    return pd.DataFrame(cols).replace([np.inf, -np.inf], np.nan)


def build_features(dataset: MarketDataset, config: ExperimentConfig) -> FeatureSet:
    features: list[pd.DataFrame] = []
    provenance: dict[str, str] = {}
    benchmark = dataset.frames[("IMOEX", config.interval)].sort_values("end")
    anchor = bar_features(benchmark, atr_period=config.atr_period, layer_b=False)
    # Не используем абсолютную цену / ATR якоря как индикатор масштаба.
    anchor = anchor.drop(columns=[c for c in anchor if c == "atr" or c.startswith("volume_")])
    for symbol in config.symbols:
        raw = dataset.frames[(symbol, config.interval)].sort_values("end")
        own = bar_features(raw, atr_period=config.atr_period, layer_b=config.layer_b)
        own = own.rename(columns={"available_at": "asof"})
        own["symbol"] = symbol
        own["close_reference"] = raw["close"].astype(float).to_numpy()
        own = point_in_time_join(own, anchor, prefix="market_")
        higher_frames: list[tuple[str, pd.DataFrame]] = []
        if config.interval == "10m":
            higher_frames.append(("h1_", dataset.frames[(symbol, "1h")]))
        if config.interval != "1d":
            h1 = dataset.frames[(symbol, "1h")]
            higher_frames.extend(
                [
                    ("h4_", aggregate_closed_bars(h1, "4h", "1h")),
                    ("d1_", dataset.frames[(symbol, "1d")]),
                ]
            )
        for prefix, higher in higher_frames:
            hf = bar_features(higher, atr_period=config.atr_period, layer_b=False)
            hf = hf.drop(columns="atr")
            # Старшим фреймам не нужны 160 дней warm-up: компактный причинный контекст.
            hf = hf[
                [c for c in hf if not any(token in c for token in ("160", "80", "volume_z_60"))]
            ]
            own = point_in_time_join(own, hf, prefix=prefix)
            provenance[prefix] = "merge_asof(backward) по exclusive close, не begin"
        if config.layer_b:
            own["relative_return_20"] = own["return_20"] - own["market_return_20"]
            # Корреляция строится на синхронизированных закрытых барах, не на ffill returns.
            exact = pd.merge(
                raw[["end", "close"]],
                benchmark[["end", "close"]],
                on="end",
                suffixes=("_asset", "_market"),
            )
            ar = np.log(exact["close_asset"].astype(float)).diff()
            mr = np.log(exact["close_market"].astype(float)).diff()
            exact["beta_60"] = ar.rolling(60).cov(mr) / mr.rolling(60).var().replace(0, np.nan)
            exact["correlation_60"] = ar.rolling(60).corr(mr)
            exact["available_at"] = exact["end"]
            own = point_in_time_join(
                own, exact[["available_at", "beta_60", "correlation_60"]], prefix="cross_"
            )
        features.append(own)
    rows = (
        pd.concat(features, ignore_index=True)
        .sort_values(["asof", "symbol"])
        .reset_index(drop=True)
    )
    excluded = {"symbol", "asof", "atr", "close_reference"}
    columns = [c for c in rows if c not in excluded and not c.startswith("audit_")]
    provenance["all"] = "rolling/ewm past-only; normalized distances; no global scaler/selection"
    return FeatureSet(
        rows=rows,
        columns=columns,
        provenance=provenance,
        warnings=[
            "Предпраздничный признак отключён: нужен версионированный исторический календарь MOEX.",
            "Исторические maxima/minima считаются на trailing windows, не на полной истории.",
        ],
    )


def rolling_correlations(
    dataset: MarketDataset, config: ExperimentConfig, window: int = 60
) -> dict[datetime, dict[tuple[str, str], Decimal]]:
    result: dict[datetime, dict[tuple[str, str], Decimal]] = {}
    series = {
        s: dataset.frames[(s, config.interval)].set_index("end")["close"].astype(float)
        for s in config.symbols
    }
    aligned = pd.DataFrame(series)
    returns = np.log(aligned).diff()  # no ffill missing asset prices/returns
    for left, right in combinations(config.symbols, 2):
        corr = returns[left].rolling(window, min_periods=window).corr(returns[right]).dropna()
        a, b = sorted((left, right))
        pair = (a, b)
        for ts, value in corr.items():
            result.setdefault(ts.to_pydatetime(), {})[pair] = Decimal(
                str(float(np.clip(value, -1, 1)))
            )
    return result
