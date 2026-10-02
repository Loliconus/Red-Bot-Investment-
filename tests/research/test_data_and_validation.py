from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.data import (
    DataQualityError,
    aggregate_closed_bars,
    normalize_iss,
    validate_candles,
)
from synthetic_trader.features import bar_features, point_in_time_join
from synthetic_trader.labels import l1_trend_filter, triple_barrier_labels
from synthetic_trader.validation import inner_split, outer_folds


def candles(count=500, interval="1h"):
    begin = pd.date_range("2020-01-01", periods=count, freq=interval, tz="UTC")
    prices = 100 + np.sin(np.arange(count) / 10) * 3
    return validate_candles(
        pd.DataFrame(
            {
                "begin": begin,
                "end": begin + pd.Timedelta(interval),
                "open": prices,
                "high": prices + 1,
                "low": prices - 1,
                "close": prices,
                "volume": np.full(count, 1000),
            }
        )
    )


def test_iss_inclusive_end_moscow_to_utc_and_open_bar_exclusion():
    records = [
        {
            "begin": "2025-01-10 10:00:00",
            "end": "2025-01-10 10:59:59",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
        },
        {
            "begin": "2025-01-10 11:00:00",
            "end": "2025-01-10 11:59:59",
            "open": 101,
            "high": 102,
            "low": 100,
            "close": 101,
            "volume": 1000,
        },
    ]
    frame = normalize_iss(records, asof=datetime(2025, 1, 10, 8, 30, tzinfo=UTC))
    assert len(frame) == 1
    assert frame.iloc[0]["end"] == pd.Timestamp("2025-01-10 08:00:00Z")
    assert frame.iloc[0]["open"] == "100"


@pytest.mark.parametrize("damage", ["duplicate", "overlap", "negative", "geometry", "naive"])
def test_data_quality_fails_instead_of_fabricating_history(damage):
    frame = candles(10)
    if damage == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif damage == "overlap":
        frame.at[1, "begin"] = frame.at[0, "begin"] + pd.Timedelta(minutes=30)
    elif damage == "negative":
        frame.at[0, "close"] = "-1"
    elif damage == "geometry":
        frame.at[0, "high"] = "1"
    elif damage == "naive":
        frame["end"] = frame["end"].dt.tz_localize(None)
    with pytest.raises(DataQualityError):
        validate_candles(frame)


def test_pit_forward_fill_only_closed_values_and_no_backward_fill():
    base = pd.DataFrame(
        {"asof": pd.date_range("2020-01-01 09:00", periods=5, freq="30min", tz="UTC")}
    )
    higher = pd.DataFrame(
        {
            "available_at": pd.to_datetime(["2020-01-01 10:00Z", "2020-01-01 11:00Z"]),
            "value": [10.0, 20.0],
        }
    )
    joined = point_in_time_join(base, higher, prefix="h1_")
    assert joined["h1_value"].iloc[:2].isna().all()
    assert joined["h1_value"].tolist()[2:] == [10.0, 10.0, 20.0]
    higher.loc[1, "value"] = 999.0
    mutated = point_in_time_join(base, higher, prefix="h1_")
    pd.testing.assert_frame_equal(joined.iloc[:4], mutated.iloc[:4])


def test_pandas3_utc_units_and_duckdb_timezone_are_canonicalized():
    base = pd.DataFrame(
        {"asof": pd.date_range("2020-01-01", periods=4, freq="h", tz="Etc/UTC").as_unit("us")}
    )
    higher = pd.DataFrame(
        {
            "available_at": pd.date_range("2020-01-01", periods=4, freq="h", tz="UTC").as_unit(
                "ns"
            ),
            "value": range(4),
        }
    )
    assert len(point_in_time_join(base, higher, prefix="h1_")) == 4


def test_h4_bucket_is_unavailable_before_close_and_gaps_are_not_filled():
    frame = candles(20)
    h4 = aggregate_closed_bars(frame, "4h", "1h")
    assert (h4["end"] <= frame["end"].max()).all()
    gapped = frame.drop(index=4).reset_index(drop=True)
    assert len(aggregate_closed_bars(gapped, "4h", "1h")) < len(h4)
    with pytest.raises(DataQualityError):
        aggregate_closed_bars(frame, "10m", "1h")


def test_feature_prefix_is_unchanged_when_future_ohlcv_changes():
    first = candles(500)
    original = bar_features(first, atr_period=14, layer_b=True)
    future = first.copy()
    for field in ("open", "high", "low", "close"):
        future.loc[400:, field] = future.loc[400:, field].astype(float).mul(10).astype(str)
    changed = bar_features(future, atr_period=14, layer_b=True)
    pd.testing.assert_frame_equal(original.iloc[:400], changed.iloc[:400])


def test_barrier_ambiguity_censoring_and_break_has_full_horizon():
    frame = candles(40)
    for field in ("open", "close"):
        frame[field] = "100"
    frame["high"], frame["low"] = "101", "99"
    frame.loc[20, "high"], frame.loc[20, "low"] = "120", "80"
    config = ExperimentConfig(horizon=4)
    labels = triple_barrier_labels(frame, config)
    event = labels.rows.loc[labels.rows["asof"] == frame.at[19, "end"]].iloc[0]
    assert event["barrier"] == "ambiguous"
    assert pd.isna(event["y_trend"]) and pd.isna(event["y_up"])
    assert event["label_end"] == frame.at[23, "end"]
    assert labels.censored == config.horizon
    assert labels.rows["asof"].max() == frame.iloc[-config.horizon - 1]["end"]


def test_direction_is_undefined_for_timeouts_not_mislabeled_down():
    frame = candles(50)
    config = ExperimentConfig(horizon=2, upper_atr="10", lower_atr="10")
    rows = triple_barrier_labels(frame, config).rows
    timeout = rows.loc[rows["barrier"] == "timeout"]
    assert len(timeout) > 0
    assert timeout["y_up"].isna().all()
    assert (timeout["y_trend"] == 0).all()


def time_rows():
    times = pd.date_range("2020-01-01", periods=500, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "asof": np.repeat(times, 3),
            "symbol": ["A", "B", "C"] * len(times),
            "label_end": np.repeat(times + pd.Timedelta(hours=12), 3),
        }
    )


@pytest.mark.parametrize("mode", ["walk_forward", "cpcv", "purged_kfold"])
def test_variable_label_spans_purge_and_same_time_asset_groups(mode):
    rows = time_rows()
    rows.loc[rows.index % 7 == 0, "label_end"] += pd.Timedelta(hours=10)
    folds = outer_folds(rows, ExperimentConfig(folds=3), mode=mode)
    for fold in folds:
        train, test = rows.iloc[fold.train], rows.iloc[fold.test]
        assert set(train["asof"]).isdisjoint(set(test["asof"]))
        assert fold.audit["temporal_leakage_free"]
        assert fold.audit["final_overlap_fraction"] == 0
        if mode == "walk_forward":
            assert train["label_end"].max() <= test["asof"].min()


def test_inner_validation_calibration_purge_all_three_heads():
    split = inner_split(time_rows())
    assert split.fit["label_end"].max() <= split.validation["asof"].min()
    assert split.validation["label_end"].max() <= split.calibration["asof"].min()
    assert not split.audit["calibration_used_for_selection"]


def test_l1_auxiliary_is_convex_piecewise_linear_and_not_a_feature():
    values = np.r_[np.arange(20) * 0.2, 4 - np.arange(20) * 0.1].astype(float)
    smoothed = l1_trend_filter(values, strength=1.0)
    assert smoothed.shape == values.shape
    assert np.abs(np.diff(smoothed, 2)).sum() <= np.abs(np.diff(values, 2)).sum() + 1e-3


@pytest.mark.parametrize(
    "payload",
    [
        {"interval": "15m"},
        {"source": "live"},
        {"symbols": ["IMOEX"]},
        {"freeze_months": 0},
        {"risk": {"direction_threshold": ".4"}},
        {"start": "2026-01-01", "end": "2026-09-30"},
        {"untrusted": "execute"},
    ],
)
def test_unsupported_contracts_do_not_reach_worker(payload):
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(payload)
