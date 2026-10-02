from dataclasses import replace
from uuid import uuid4

import numpy as np
import pytest

from synthetic_trader.config import ExperimentConfig
from synthetic_trader.data import demo_dataset
from synthetic_trader.models import ProbabilityBundle, fit_bundle
from synthetic_trader.pipeline import prepare_rows, run_experiment, run_final
from synthetic_trader.regime import FilteredMarkovRegime
from synthetic_trader.storage import FinalAlreadyConsumedError, SnapshotStore, write_json
from synthetic_trader.validation import outer_folds


@pytest.fixture(scope="module")
def small_config():
    return ExperimentConfig(interval="1d", iterations=40, folds=2, bootstrap_reps=100, cpcv=True)


@pytest.fixture(scope="module")
def sample_rows(small_config):
    data = demo_dataset(small_config)
    features, rows, _ = prepare_rows(data, small_config)
    return data, features, rows


def test_filtered_regime_prefix_does_not_change_with_future(sample_rows):
    _, _, rows = sample_rows
    fit = rows.iloc[:900]
    regime = FilteredMarkovRegime.fit(fit, 42)
    prefix = regime.transform(rows.iloc[:1500])
    future = rows.copy()
    future.loc[1500:, "market_return_1"] = 0.99
    future.loc[1500:, "market_rv_20"] = 0.99
    transformed = regime.transform(future).iloc[:1500]
    np.testing.assert_allclose(
        prefix[["regime_trend", "regime_range", "regime_panic"]],
        transformed[["regime_trend", "regime_range", "regime_panic"]],
    )


def test_model_fits_only_train_and_exports_calibration_without_pickle(
    sample_rows, small_config, tmp_path
):
    _, features, rows = sample_rows
    fold = outer_folds(rows, small_config)[0]
    bundle = fit_bundle(rows.iloc[fold.train], features.columns, small_config)
    assert bundle.audit["inner_split"]["calibration_used_for_selection"] is False
    assert bundle.audit["test_used_for_fit_or_selection"] is False
    assert all(
        c not in {"symbol", "asof", "atr", "close_reference"} and not c.startswith("audit_")
        for c in bundle.columns
    )
    bundle.save(tmp_path / "model")
    restored = ProbabilityBundle.load(tmp_path / "model")
    np.testing.assert_allclose(
        bundle.predict(rows)[["p_trend", "p_up", "p_break"]],
        restored.predict(rows)[["p_trend", "p_up", "p_break"]],
    )
    assert not list(tmp_path.rglob("*.pkl"))


def test_dataset_is_immutable_and_model_parameters_do_not_change_snapshot_id(
    small_config, tmp_path
):
    data = demo_dataset(small_config)
    store = SnapshotStore(tmp_path)
    first = store.save(data, small_config)
    second = store.save(data, small_config.model_copy(update={"depth": 6}))
    assert first["dataset_id"] == second["dataset_id"]
    assert first["cutoff"] == second["cutoff"]
    dev = store.development(first["dataset_id"])
    assert max(frame["end"].max() for frame in dev.frames.values()).isoformat() <= first["cutoff"]
    with pytest.raises(ValueError, match="закрыты"):
        store.frozen(first["dataset_id"], run_id="unapproved")
    file = store.path(first["dataset_id"]) / first["files"][0]["path"]
    file.write_bytes(file.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="checksum"):
        store.development(first["dataset_id"])


def test_full_pipeline_cpcv_reports_real_calculations_never_reads_frozen(
    small_config, tmp_path, monkeypatch
):
    run_id = uuid4().hex
    root = tmp_path / "research"
    directory = root / "runs" / run_id
    write_json(directory / "config.json", small_config.model_dump(mode="json"))
    reads = []
    original = SnapshotStore._read_part

    def spy(self, dataset_id, part):
        reads.append(part)
        assert part != "frozen"
        return original(self, dataset_id, part)

    monkeypatch.setattr(SnapshotStore, "_read_part", spy)
    report = run_experiment(root, run_id, small_config, lambda *_: None)
    assert reads and set(reads) == {"development"}
    assert report["live_enabled"] is False
    assert report["source"] == "demo"
    assert report["cpcv"]["computed"] and len(report["cpcv"]["paths"]) == 3
    assert report["cpcv"]["combinations"] == 6
    assert report["statistics"]["trials"] >= len(report["candidates"])
    assert report["label_audit"]["rows"] > 300
    assert not report["final_oos"]["consumed"]
    assert report["metrics"]["trades"] > 0
    assert all(fold["outer"]["final_overlap_fraction"] == 0 for fold in report["folds"])
    assert (directory / "report.json").is_file() and (directory / "predictions.parquet").is_file()
    assert all(key in report["benchmarks"] for key in ("imoex", "ma", "rsi"))
    assert all(not column.startswith("retrospective") for column in report["selected_features"])


def test_final_is_once_no_fit_no_selection_and_failed_reveal_stays_consumed(
    small_config, tmp_path, monkeypatch
):
    run_id = uuid4().hex
    root = tmp_path / "research"
    directory = root / "runs" / run_id
    write_json(directory / "config.json", small_config.model_dump(mode="json"))
    report = run_experiment(
        root, run_id, small_config.model_copy(update={"cpcv": False}), lambda *_: None
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("fit called on final")

    monkeypatch.setattr("synthetic_trader.pipeline.fit_bundle", forbidden)
    final = run_final(root, run_id, lambda *_: None)
    assert final["consumed"] and final["no_fit_no_selection_no_recalibration"]
    assert final["model_hash"] == report["candidate_model_hash"]
    with pytest.raises(FinalAlreadyConsumedError):
        run_final(root, run_id, lambda *_: None)
    store = SnapshotStore(root)
    with pytest.raises(FinalAlreadyConsumedError):
        store.development(report["dataset"]["dataset_id"])


def test_modified_cbm_cannot_open_final_then_runtime_failure_stays_consumed(
    small_config, tmp_path, monkeypatch
):
    run_id = uuid4().hex
    root = tmp_path / "research"
    directory = root / "runs" / run_id
    config = small_config.model_copy(update={"cpcv": False})
    write_json(directory / "config.json", config.model_dump(mode="json"))
    report = run_experiment(root, run_id, config, lambda *_: None)
    cbm = next((directory / "model").glob("*.cbm"))
    content = cbm.read_bytes()
    cbm.write_bytes(content + b"changed")
    with pytest.raises(ValueError, match="изменены"):
        run_final(root, run_id, lambda *_: None)
    store = SnapshotStore(root)
    assert not store.ledger.frozen_state(report["dataset"]["study_id"])["consumed"]
    cbm.write_bytes(content)

    def fail(*args, **kwargs):
        raise RuntimeError("after reveal")

    monkeypatch.setattr("synthetic_trader.pipeline.run_backtest", fail)
    with pytest.raises(RuntimeError, match="after reveal"):
        run_final(root, run_id, lambda *_: None)
    assert store.ledger.frozen_state(report["dataset"]["study_id"])["consumed"]
    with pytest.raises(FinalAlreadyConsumedError):
        run_final(root, run_id, lambda *_: None)


def test_financial_atr_is_decimal_causal_and_not_prediction_feature(sample_rows, small_config):
    from decimal import Decimal

    from synthetic_trader.backtest import decimal_atr_history, signals_from_predictions

    data, _, rows = sample_rows
    history = decimal_atr_history(data, small_config)
    asset = small_config.symbols[0]
    row = rows.loc[rows["symbol"] == asset].iloc[[0]].copy()
    row["atr"] = float("nan")  # This ML feature must not construct a financial stop.
    row["p_trend"], row["p_up"], row["p_break"], row["regime_panic"] = 0.8, 0.8, 0.1, 0.0
    row["regime"] = "trend"
    market_signal = signals_from_predictions(row, history)[0]
    assert isinstance(market_signal.atr, Decimal) and market_signal.atr > 0
    changed = data.frames[(asset, small_config.interval)].copy()
    end = row["asof"].iloc[0]
    future = changed["end"] > end
    for column in ("open", "high", "low", "close"):
        changed.loc[future, column] = changed.loc[future, column].map(
            lambda value: str(Decimal(value) * 10)
        )
    modified = replace(data, frames={**data.frames, (asset, small_config.interval): changed})
    next_history = decimal_atr_history(modified, small_config)
    assert history[(asset, end.to_pydatetime())] == next_history[(asset, end.to_pydatetime())]
