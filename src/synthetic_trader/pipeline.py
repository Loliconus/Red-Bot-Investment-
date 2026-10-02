"""Сквозной baseline эксперимент. Никаких вызовов брокера или LIVE promotion."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import platform
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from purgedcv import reconstruct_paths
from sklearn.metrics import brier_score_loss

from synthetic_trader.backtest import candidate_thresholds, run_backtest, run_benchmarks
from synthetic_trader.config import FEATURE_VERSION, LABEL_VERSION, ExperimentConfig
from synthetic_trader.data import MarketDataset, demo_dataset, load_moex
from synthetic_trader.features import FeatureSet, build_features, rolling_correlations
from synthetic_trader.labels import l1_trend_filter, triple_barrier_labels
from synthetic_trader.models import (
    HEAD_NAMES,
    ProbabilityBundle,
    bundle_hash,
    fit_bundle,
    reliability,
)
from synthetic_trader.statistics import (
    daily_equity,
    daily_returns,
    multiple_testing,
    performance_metrics,
    population_stability_index,
)
from synthetic_trader.storage import (
    SnapshotStore,
    digest,
    file_digest,
    json_safe,
    research_job,
    write_json,
    write_parquet,
)
from synthetic_trader.validation import outer_folds, time_axis

type Progress = Callable[[str, int, str], None]


def prepare_rows(
    dataset: MarketDataset, config: ExperimentConfig
) -> tuple[FeatureSet, pd.DataFrame, dict[str, Any]]:
    features = build_features(dataset, config)
    labels: list[pd.DataFrame] = []
    ambiguous, censored = 0, 0
    for symbol in config.symbols:
        result = triple_barrier_labels(dataset.frames[(symbol, config.interval)], config)
        result.rows["symbol"] = symbol
        labels.append(result.rows)
        ambiguous += result.ambiguous
        censored += result.censored
    rows = features.rows.merge(
        pd.concat(labels, ignore_index=True),
        on=["asof", "symbol"],
        how="inner",
        validate="one_to_one",
    )
    before = len(rows)
    # Убираем только warm-up / undefined features, не балансируем / не перемешиваем данные.
    rows = (
        rows.dropna(subset=[*features.columns, "atr", "rv_20"])
        .sort_values(["asof", "symbol"])
        .reset_index(drop=True)
    )
    if rows.empty:
        raise ValueError(
            "После PIT warm-up нет наблюдений; проверьте закрытые daily/4H бары и объёмы"
        )
    return (
        features,
        rows,
        {
            "ambiguous_events": ambiguous,
            "censored_tail_events": censored,
            "warmup_removed": before - len(rows),
            "rows": len(rows),
            "unique_times": rows["asof"].nunique(),
            "trend_target": "ATR barrier touch before H timeout; proxy for directional movement, not a proof of statistical trend",
            "up_target": "upper barrier first, conditional on unambiguous trend events",
            "break_target": "opposite ATR barrier within FULL H against causal EMA10/40 bias, entry at next open",
            "label_span": "max(triple barrier span, full break horizon) = H; tail censored, never negative-labeled",
            "feature_provenance": features.provenance,
        },
    )


def probability_metrics(predicted: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for target, name in HEAD_NAMES.items():
        valid = predicted[target].notna()
        labels = predicted.loc[valid, target].to_numpy(dtype=int)
        p = predicted.loc[valid, f"p_{name}"].to_numpy(dtype=float)
        raw = predicted.loc[valid, f"raw_{name}"].to_numpy(dtype=float)
        result[name] = {
            "samples": len(labels),
            "positives": int(labels.sum()),
            "brier": float(brier_score_loss(labels, p)) if len(labels) else None,
            "raw_brier": float(brier_score_loss(labels, raw)) if len(labels) else None,
            "constant_train_brier": float(
                brier_score_loss(labels, predicted.loc[valid, f"prior_{name}"].to_numpy())
            )
            if len(labels)
            else None,
            "reliability": reliability(labels, p) if len(labels) else [],
            "conditional_on_trend": name == "up",
        }
    return result


def code_fingerprint() -> str:
    """Фиксируем фактические исходники, включая uncommitted изменения, а не только Git HEAD."""
    import core.backtest.probability as simulator_module
    import core.domain.probability as domain_module
    import core.risk.probability as risk_module

    names = (
        "config",
        "data",
        "features",
        "labels",
        "validation",
        "selection",
        "regime",
        "models",
        "backtest",
        "statistics",
        "storage",
        "pipeline",
    )
    files = {name: file_digest(Path(__file__).parent / f"{name}.py") for name in names}
    for name, module in (
        ("core_simulator", simulator_module),
        ("core_domain", domain_module),
        ("core_risk", risk_module),
    ):
        filename = module.__file__
        if filename is None:
            raise ValueError("Нужны доступные исходники для фиксации модели")
        files[name] = file_digest(Path(filename))
    return digest(files)


def environment_metadata() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for name in (
        "numpy",
        "pandas",
        "scipy",
        "scikit-learn",
        "catboost",
        "purgedcv",
        "aiomoex",
        "arch",
        "duckdb",
        "osqp",
    ):
        packages[name] = importlib.metadata.version(name)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — фиксированная read-only команда
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        commit = "unavailable"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "git_commit": commit,
        "feature_version": FEATURE_VERSION,
        "label_version": LABEL_VERSION,
    }


def _dataset(
    config: ExperimentConfig, store: SnapshotStore
) -> tuple[MarketDataset, dict[str, Any]]:
    store.ledger.register_study(config)
    if config.dataset_id:
        manifest = store.manifest(config.dataset_id)
        for key in ("source", "interval", "symbols", "start", "end", "freeze_months"):
            expected = config.model_dump(mode="json")[key]
            if key == "symbols":
                if sorted(expected) != sorted(manifest[key]):
                    raise ValueError("Config universe не совпадает с immutable dataset")
            elif manifest[key] != expected:
                raise ValueError(f"Config {key} не совпадает с immutable dataset")
    else:
        loaded = demo_dataset(config) if config.source == "demo" else asyncio.run(load_moex(config))
        manifest = store.save(loaded, config)
    return store.development(manifest["dataset_id"]), manifest


def _curve(result: Any, benchmarks: dict[str, Any]) -> list[dict[str, Any]]:
    primary = daily_equity(result)
    capital = float(result.equity[0].equity)
    curves: dict[str, dict[str, float]] = {}
    for key, report in benchmarks.items():
        wealth = capital
        values: dict[str, float] = {}
        for point in report["daily_returns"]:
            wealth *= 1 + point["return"]
            values[point["date"]] = wealth
        curves[key] = values
    return [
        {
            "date": date.isoformat(),
            "strategy": float(value),
            **{key: values.get(date.isoformat()) for key, values in curves.items()},
        }
        for date, value in primary.items()
    ]


def _cpcv(
    dataset: MarketDataset,
    config: ExperimentConfig,
    rows: pd.DataFrame,
    columns: list[str],
    correlations: Any,
    progress: Progress,
) -> dict[str, Any]:
    folds = outer_folds(rows, config, mode="cpcv")
    predictions: list[pd.DataFrame] = []
    time_tests: list[Any] = []
    markers: list[Any] = []
    audit: list[dict[str, Any]] = []
    axis = time_axis(rows)
    for index, fold in enumerate(folds):
        progress(
            "cpcv",
            56 + int(20 * index / len(folds)),
            f"CPCV combination {index + 1}/{len(folds)}; независимое fit/calibration",
        )
        bundle = fit_bundle(rows.iloc[fold.train], columns, config)
        predicted = bundle.predict(rows)
        test = predicted.iloc[fold.test].copy()
        predictions.append(test)
        test_times = np.flatnonzero(axis["asof"].isin(test["asof"]).to_numpy())
        time_tests.append(test_times)
        markers.append(np.full(len(test_times), index, dtype=float))
        audit.append({"outer": fold.audit, "model": bundle.audit})
    # Используем canonical library reconstruction, не mean нескольких fits на test.
    paths = reconstruct_paths(markers, time_tests, config.cpcv_groups, 2, len(axis))
    metrics: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        pieces = [
            prediction.loc[prediction["asof"].isin(axis.iloc[np.flatnonzero(path == f)]["asof"])]
            for f, prediction in enumerate(predictions)
        ]
        predicted = pd.concat(pieces).sort_values(["asof", "symbol"]).reset_index(drop=True)
        if len(predicted) != len(rows) or predicted[["asof", "symbol"]].duplicated().any():
            raise ValueError("CPCV path reconstruction потеряла / продублировала моменты")
        result = run_backtest(dataset, config, predicted, correlations=correlations)
        metrics.append(
            {
                "path": index + 1,
                **performance_metrics(result),
                "probabilities": probability_metrics(predicted),
            }
        )
    return {
        "computed": True,
        "groups": config.cpcv_groups,
        "test_groups": 2,
        "combinations": len(folds),
        "paths": metrics,
        "fold_audits": audit,
        "warning": "Пути комбинаторные, зависимы и обучаются также на будущих train-группах. Не заменяют chronological walk-forward / final OOS.",
    }


def _checks(
    config: ExperimentConfig,
    metrics: dict[str, Any],
    statistics: dict[str, Any],
    folds: list[dict[str, Any]],
    cpcv: dict[str, Any],
) -> list[dict[str, Any]]:
    dsr = (statistics.get("dsr") or {}).get("dsr")
    pbo = (statistics.get("pbo") or {}).get("probability")
    spa = (statistics.get("spa") or {}).get("p_value")
    tests = [
        ("real_data", "Реальные данные, не DEMO", config.source != "demo"),
        (
            "pit",
            "PIT / full label-span purging",
            all(f["outer"].get("temporal_leakage_free") for f in folds),
        ),
        (
            "calibration",
            "Три головы откалиброваны на отдельном временном куске",
            all(
                all(
                    v == "isotonic_separate_temporal_slice"
                    for v in f["model"]["calibration_status"].values()
                )
                for f in folds
            ),
        ),
        (
            "probability_quality",
            "OOS Brier каждой головы не хуже постоянного train-prior",
            all(
                all(
                    score["brier"] is not None
                    and score.get("constant_train_brier") is not None
                    and score["brier"] <= score["constant_train_brier"]
                    for score in fold["probabilities"].values()
                )
                for fold in folds
            ),
        ),
        (
            "stability",
            "Устойчивые признаки без exploratory fallback",
            all(not f["model"]["exploratory_fallback"] for f in folds),
        ),
        ("sample", "Не менее 30 OOS сделок", metrics["trades"] >= 30),
        ("dsr", "DSR ≥ 0.95", dsr is not None and dsr >= 0.95),
        ("pbo", "PBO ≤ 0.20", pbo is not None and pbo <= 0.20),
        ("spa", "Hansen SPA p < 0.05 против IMOEX", spa is not None and spa < 0.05),
        (
            "drawdown",
            "Просадка ≤ 20% / kill switch не сработал",
            metrics["max_drawdown"] is not None
            and metrics["max_drawdown"] >= -0.20
            and not metrics["kill_reason"],
        ),
        ("cpcv", "CPCV рассчитан (не только один путь)", bool(cpcv.get("computed"))),
        ("final", "Одноразовый final OOS ещё не раскрыт", False),
        ("data_audit", "PIT universe, corporate actions / total return проверены", False),
    ]
    return [{"id": key, "label": label, "passed": bool(passed)} for key, label, passed in tests]


def run_experiment(
    root: Path, run_id: str, config: ExperimentConfig, progress: Progress
) -> dict[str, Any]:
    with research_job(root, run_id):
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if config.mlflow_tracking and importlib.util.find_spec("mlflow") is None:
            raise ValueError("MLflow tracking требует uv sync --extra mlops")
        source_hash = code_fingerprint()
        store = SnapshotStore(root)
        progress("data", 5, "Загрузка / проверка immutable history; брокер отключён")
        dataset, manifest = _dataset(config, store)
        thresholds = candidate_thresholds(config)
        trials = store.ledger.register_trials(
            manifest["study_id"], run_id, config, [str(t) for t in thresholds]
        )
        progress("features", 14, "PIT: 4H / daily только после закрытия; frozen данные не читаются")
        features, rows, labels = prepare_rows(dataset, config)
        cutoff = pd.Timestamp(manifest["cutoff"])
        if (rows["label_end"] > cutoff).any():
            raise ValueError("Label span попал в frozen holdout")
        write_parquet(rows, run_dir / "features.parquet")
        if config.l1_auxiliary:
            auxiliary: list[pd.DataFrame] = []
            for symbol in config.symbols:
                frame = dataset.frames[(symbol, config.interval)].iloc[-5000:]
                trend = l1_trend_filter(
                    np.log(frame["close"].astype(float).to_numpy()), strength=0.1
                )
                auxiliary.append(
                    pd.DataFrame(
                        {
                            "asof": frame["end"].to_numpy(),
                            "symbol": symbol,
                            "retrospective_l1_trend": trend,
                            "slope": np.r_[np.nan, np.diff(trend)],
                        }
                    )
                )
            write_parquet(pd.concat(auxiliary), run_dir / "auxiliary_labels.parquet")
        correlations = rolling_correlations(dataset, config)
        folds = outer_folds(rows, config)
        oos: list[pd.DataFrame] = []
        audits: list[dict[str, Any]] = []
        for index, fold in enumerate(folds):
            progress(
                "walk_forward",
                22 + int(28 * index / len(folds)),
                f"Fold {index + 1}/{len(folds)}: Elastic Net → stability → SHAP → CatBoost → calibration",
            )
            bundle = fit_bundle(rows.iloc[fold.train], features.columns, config)
            all_prediction = bundle.predict(rows)
            test = all_prediction.iloc[fold.test].copy()
            test["fold"] = index + 1
            oos.append(test)
            audits.append(
                {
                    "fold": index + 1,
                    "outer": fold.audit,
                    "model": bundle.audit,
                    "probabilities": bundle.evaluate(test),
                    "test_start": test["asof"].min(),
                    "test_end": test["asof"].max(),
                }
            )
        predicted = pd.concat(oos).sort_values(["asof", "symbol"]).reset_index(drop=True)
        if predicted[["asof", "symbol"]].duplicated().any():
            raise ValueError("Walk-forward предсказания перекрываются")
        write_parquet(predicted, run_dir / "predictions.parquet")
        progress(
            "backtest",
            52,
            "Общий событийный портфель, next-open execution, fees/slippage, trailing, корреляции",
        )
        results = {
            str(t): run_backtest(dataset, config, predicted, threshold=t, correlations=correlations)
            for t in thresholds
        }
        primary = results[str(config.risk.trend_threshold)]
        oos_rows = rows.merge(
            predicted[["asof", "symbol"]], on=["asof", "symbol"], validate="one_to_one"
        )
        benchmarks, imoex = run_benchmarks(dataset, config, oos_rows, primary, correlations)
        return_matrix = pd.DataFrame({key: daily_returns(value) for key, value in results.items()})
        imoex = imoex.reindex(return_matrix.index)
        if imoex.isna().any():
            raise ValueError("Бенчмарк IMOEX не покрывает общий OOS-календарь")
        cpcv = (
            _cpcv(dataset, config, rows, features.columns, correlations, progress)
            if config.cpcv
            else {"computed": False, "reason": "Отключён пользователем; research не проходит gate"}
        )
        progress("statistics", 78, "CSCV PBO, deflated daily Sharpe, White RC / Hansen SPA")
        store.ledger.record_sharpes(
            run_id,
            {
                name: float(series.mean() / series.std(ddof=0))
                if series.std(ddof=0) > 1e-12
                else None
                for name, series in return_matrix.items()
            },
        )
        population = store.ledger.trial_population(manifest["study_id"])
        statistics = multiple_testing(
            return_matrix,
            imoex,
            primary=str(config.risk.trend_threshold),
            trials=max(trials, population["trials"]),
            trial_variance=population["variance"],
            repetitions=config.bootstrap_reps,
            seed=config.seed,
        )
        statistics["trial_population"] = population
        progress(
            "registry", 88, "Фиксация final-candidate bundle исключительно на development history"
        )
        # Не используем outer OOS для выбора winner; зафиксированный конфиг fit заново
        # на development, с отдельными internal validation + calibration + purge.
        candidate = fit_bundle(rows, features.columns, config)
        candidate.save(run_dir / "model")
        psi: dict[str, float | None] = {}
        candidate_context = candidate.regime.transform(rows)
        inner = candidate.audit["inner_split"]
        reference = candidate_context.loc[
            candidate_context["label_end"] <= pd.Timestamp(inner["fit_end"])
        ]
        observed = candidate_context.loc[candidate_context["asof"].isin(predicted["asof"])]
        for col in candidate.columns:
            psi[col] = population_stability_index(
                candidate.reference_bins[col],
                reference[col].to_numpy(dtype=float),
                observed[col].to_numpy(dtype=float),
            )
        metrics = performance_metrics(primary)
        report = {
            "run_id": run_id,
            "source": config.source,
            "config": config.model_dump(mode="json"),
            "dataset": manifest,
            "environment": environment_metadata(),
            "feature_version": FEATURE_VERSION,
            "feature_set_hash": digest(
                {"version": FEATURE_VERSION, "columns": features.columns, "layer_b": config.layer_b}
            ),
            "candidate_model_hash": bundle_hash(run_dir / "model"),
            "source_hash": source_hash,
            "metrics": metrics,
            "probabilities": probability_metrics(predicted),
            "folds": audits,
            "label_audit": labels,
            "statistics": statistics,
            "cpcv": cpcv,
            "benchmarks": benchmarks,
            "candidates": [
                {"trend_threshold": key, "metrics": performance_metrics(value)}
                for key, value in results.items()
            ],
            "selection_basis": "Risk threshold заранее фиксирован; не выбирается по outer OOS и тем более final",
            "equity_curve": _curve(primary, benchmarks),
            "trades": [asdict(t) for t in primary.trades],
            "latest": predicted.sort_values("asof").groupby("symbol").tail(1).to_dict("records"),
            "probability_series": {
                symbol: group.tail(350)[["asof", "p_trend", "p_up", "p_break", "regime"]].to_dict(
                    "records"
                )
                for symbol, group in predicted.groupby("symbol")
            },
            "selected_features": candidate.columns,
            "feature_importance": candidate.audit["shap"],
            "monitoring": {
                "psi": psi,
                "drift_alert": any(v is not None and v > 0.25 for v in psi.values()),
                "note": "Retrospective fold/reference PSI; не online production monitoring и не автоматическое LIVE-переобучение",
            },
            "checks": _checks(config, metrics, statistics, audits, cpcv),
            "warnings": [
                *dataset.warnings,
                *features.warnings,
                "P(trend) — barrier-event proxy; P(break) — stop/reversal proxy, не доказанная вероятность структурного излома.",
                "Long-only исполнение: bearish вероятность означает отказ/выход, а не short. IMOEX никогда не торгуется.",
                "Финальный holdout ещё закрыт. Ни Sharpe, ни accuracy на исследовательской истории не разрешают LIVE.",
                "OHLC исполнения не моделируют стакан, очередь, funding, рыночное воздействие и latency; daily kill не гарантирует лимит при гэпе.",
                "PSI в этом отчёте не является независимым OOS тестом: final-candidate fitted на development после walk-forward.",
            ],
            "registry_stage": "research",
            "live_enabled": False,
            "final_oos": {"consumed": False, "cutoff": manifest["cutoff"]},
        }
        if code_fingerprint() != source_hash:
            raise ValueError(
                "Исходники изменились во время эксперимента. Повторите запуск с фиксированным кодом."
            )
        write_json(run_dir / "report.json", report)
        write_report_markdown(run_dir / "report.md", report)
        if config.mlflow_tracking:
            from synthetic_trader.mlops import log_mlflow_run

            log_mlflow_run(root, run_dir, report)
        store.ledger.register_candidate(
            run_id, manifest["study_id"], digest(report), report["candidate_model_hash"]
        )
        progress("completed", 100, "Исследование завершено. Final OOS закрыт; real orders OFF")
        return cast("dict[str, Any]", json_safe(report))


def run_final(root: Path, run_id: str, progress: Progress) -> dict[str, Any]:
    import json

    with research_job(root, run_id):
        run_dir = root / "runs" / run_id
        original = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        config = ExperimentConfig.model_validate(original["config"])
        store = SnapshotStore(root)
        manifest = original["dataset"]
        store.ledger.assert_development_open(manifest["study_id"])
        current_hash = bundle_hash(run_dir / "model")
        store.ledger.assert_candidate(run_id, digest(original), current_hash)
        if original["environment"]["packages"] != environment_metadata()["packages"]:
            raise ValueError("Версии научных библиотек изменены; final остаётся закрыт")
        if original.get("source_hash") != code_fingerprint():
            raise ValueError(
                "Код изменён после фиксации модели. Final не раскрыт; нужен новый development прогон."
            )
        if digest(store.manifest(manifest["dataset_id"])) != digest(manifest):
            raise ValueError("Immutable manifest изменён; final не раскрыт")
        # Claim ДО чтения frozen, даже если последующее вычисление завершится ошибкой.
        store.ledger.reveal_once(manifest["study_id"], run_id)
        progress(
            "final_oos",
            10,
            "Одноразовое раскрытие зафиксировано; никакого fit / selection / recalibration",
        )
        frozen = store.frozen(manifest["dataset_id"], run_id=run_id)
        development = store.revealed_development(manifest["dataset_id"], run_id=run_id)
        merged = MarketDataset(
            frames={
                key: pd.concat([development.frames[key], frame], ignore_index=True)
                for key, frame in frozen.frames.items()
            },
            instruments=frozen.instruments,
            source=frozen.source,
            warnings=frozen.warnings,
        )
        features, rows, labels = prepare_rows(merged, config)
        del features  # final запрещено отбирать признаки
        bundle = ProbabilityBundle.load(run_dir / "model")
        predictions = bundle.predict(rows)
        predictions = predictions.loc[predictions["asof"] > pd.Timestamp(manifest["cutoff"])].copy()
        if predictions.empty:
            raise ValueError("Нет полноценных final OOS событий после warm-up")
        progress(
            "final_oos", 60, "Исполнение фиксированной модели и risk policy на untouched holdout"
        )
        correlation = rolling_correlations(merged, config)
        result = run_backtest(merged, config, predictions, correlations=correlation)
        oos_rows = rows.merge(
            predictions[["asof", "symbol"]], on=["asof", "symbol"], validate="one_to_one"
        )
        benchmarks, _ = run_benchmarks(merged, config, oos_rows, result, correlation)
        report = {
            "consumed": True,
            "run_id": run_id,
            "source": config.source,
            "model_hash": original["candidate_model_hash"],
            "dataset_id": manifest["dataset_id"],
            "metrics": performance_metrics(result),
            "probabilities": bundle.evaluate(predictions),
            "benchmarks": benchmarks,
            "equity_curve": _curve(result, benchmarks),
            "label_audit": labels,
            "no_fit_no_selection_no_recalibration": True,
            "live_enabled": False,
            "warning": "Это последний final тест study. DEMO не доказательство; даже успешный real test требует data/execution audit и shadow.",
        }
        write_parquet(predictions, run_dir / "final_predictions.parquet")
        write_json(run_dir / "final.json", report)
        progress("completed", 100, "Final OOS раскрыт и навсегда помечен использованным")
        return cast("dict[str, Any]", json_safe(report))


def write_report_markdown(path: Path, report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    lines = [
        "# Синтетический трейдер · исследовательский отчёт",
        "",
        f"Run: `{report['run_id']}` · Source: **{report['source'].upper()}** · LIVE: **OFF**",
        "",
        "## Идентичность эксперимента",
        f"- Dataset SHA256: `{report['dataset']['dataset_id']}`",
        f"- Feature set: `{report['feature_set_hash']}`",
        f"- Model bytes SHA256: `{report['candidate_model_hash']}`",
        f"- Source fingerprint: `{report['source_hash']}`",
        f"- Python: {report['environment']['python']}",
        f"- Frozen cutoff: {report['dataset']['cutoff']} (не читался)",
        "",
        "## Walk-forward / net",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in metrics.items() if key != "rejections")
    lines.extend(["", "## Multiple-testing diagnostics (daily net)", ""])
    statistics = report["statistics"]
    lines.append(f"- Accumulated trials: {statistics['trials']}")
    for test, key in (
        ("dsr", "dsr"),
        ("pbo", "probability"),
        ("spa", "p_value"),
        ("reality_check", "p_value"),
    ):
        result = statistics.get(test)
        lines.append(f"- {test}: {result[key] if result else 'unavailable (не PASS)'}")
    lines.extend(
        [
            "",
            "## OOS probability quality",
            "",
            "| Head | Brier | Raw | Constant train prior | n |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for head, score in report["probabilities"].items():
        lines.append(
            f"| {head} | {score['brier']} | {score['raw_brier']} | {score.get('constant_train_brier')} | {score['samples']} |"
        )
    cpcv = report["cpcv"]
    lines.extend(["", "## CPCV paths (зависимые diagnostics)", ""])
    if cpcv.get("computed"):
        for point in cpcv["paths"]:
            lines.append(
                f"- Path {point['path']}: net {point['net_return']}, Sharpe {point['sharpe']}, DD {point['max_drawdown']}, trades {point['trades']}"
            )
    else:
        lines.append(cpcv.get("reason", "Не рассчитан"))
    lines.extend(["", "## Gate (не разрешение торговать)", ""])
    lines.extend(
        f"- {'PASS' if c['passed'] else 'NOT PASSED'}: {c['label']}" for c in report["checks"]
    )
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {warning}" for warning in report["warnings"])
    lines.extend(
        [
            "",
            "Полные настройки, Brier/reliability, PBO/DSR/SPA/RC, folds и ledger находятся в report.json.",
            "Ссылки и уточнения методологии: docs/research/06-synthetic-trader.md.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
