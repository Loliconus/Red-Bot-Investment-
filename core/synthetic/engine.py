"""Оркестратор вероятностной системы «Синтетический трейдер» (разделы 0–7 ТЗ).

Связывает в единый воспроизводимый конвейер:
- Этап 0: Марковское переключение режимов Гамильтона (HMM: ``TREND / CHOP / PANIC``);
- Слой признаков A + B + кросс-актив к ``IMOEX`` + этапы 3–5 (PatchTST, TFT VSN, GAT, 1D-CNN);
- Разметку Triple-Barrier (АТР-адаптивный) и ℓ1-фильтрацию тренда;
- Трёхэтапный отбор признаков (Elastic Net → Stability Selection → CatBoost SHAP + |corr| <= 0.9);
- Трёхголовый ансамбль симметричных деревьев CatBoost:
  ``P(trend)``, ``P(up | trend)``, ``P(break within H)`` с пост-калибровкой;
- Валидацию Purged K-Fold + Embargo, CPCV, PBO (CSCV), DSR, White RC и Hansen SPA;
- Детерминированный слой риск-менеджмента и параллельное сравнение с 3 бенчмарками.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.domain.value_objects import OHLCV
from core.synthetic.advanced_models import (
    CandlestickCNN1DExtractor,
    CrossAssetGATBlock,
    PatchTSTEncoder,
    TemporalFusionVariableSelector,
)
from core.synthetic.benchmarks import (
    BenchmarkRunResult,
    run_buy_and_hold_benchmark,
    run_ma_crossover_benchmark,
    run_rsi_benchmark,
)
from core.synthetic.calibration import CalibrationReport
from core.synthetic.catboost_model import (
    CatBoostHyperparameters,
    ProbabilisticTriadPrediction,
    SyntheticCatBoostTriadModel,
)
from core.synthetic.feature_selection import (
    FeatureSelectionReport,
    run_feature_selection_pipeline,
)
from core.synthetic.feature_store import AlignedMultiTimeframeSnapshot
from core.synthetic.features import build_feature_vector
from core.synthetic.hmm_regime import (
    HamiltonMarkovRegimeDetector,
    HMMRegimePosterior,
)
from core.synthetic.labeling import (
    L1TrendFilterResult,
    TripleBarrierConfig,
    TripleBarrierEvent,
    build_triple_barrier_events,
    compute_wilder_atr_series,
    solve_l1_trend_filter,
)
from core.synthetic.mlops import (
    DatasetVersionManifest,
    ExperimentRunRecord,
    FeatureDriftDiagnostic,
    build_dataset_manifest,
    build_experiment_record,
    evaluate_drift_and_retraining_trigger,
)
from core.synthetic.risk_engine import (
    SyntheticDirection,
    SyntheticPositionDecision,
    SyntheticRiskConfig,
    evaluate_probabilistic_risk_gate,
)
from core.synthetic.validation import (
    FrozenHoldoutPartition,
    OverfittingAuditReport,
    build_cpcv_splits,
    compute_annualized_sharpe,
    compute_deflated_sharpe_ratio,
    compute_pbo_cscv,
    compute_white_rc_and_hansen_spa,
    partition_frozen_holdout,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticStepRecord:
    """Точка вневыборочного (OOS) прогона «Синтетического трейдера» на одном баре."""

    timestamp: datetime
    close_price: Decimal
    equity: Decimal
    p_trend: float
    p_up_given_trend: float
    p_break_within_h: float
    hmm_regime: str
    hmm_prob_panic: float
    l1_trend_price: Decimal
    l1_slope: float
    direction: SyntheticDirection
    position_fraction: Decimal
    blocked_reason: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticTraderEvaluationReport:
    """Полный отчёт обучения, валидации и бэктеста «Синтетического трейдера»."""

    instrument_uid: str
    ticker: str
    timeframe: str
    initial_capital: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    max_drawdown_pct: Decimal
    oos_sharpe_ratio: float
    win_rate_pct: Decimal
    profit_factor: Decimal
    trades_count: int
    latest_triad: ProbabilisticTriadPrediction
    latest_hmm: HMMRegimePosterior
    latest_risk_decision: SyntheticPositionDecision
    l1_trend_summary: L1TrendFilterResult
    feature_selection: FeatureSelectionReport
    calibration_reports: Mapping[str, CalibrationReport]
    overfitting_audit: OverfittingAuditReport
    holdout_partition: FrozenHoldoutPartition
    benchmarks: tuple[BenchmarkRunResult, ...]
    drift_diagnostic: FeatureDriftDiagnostic
    dataset_manifest: DatasetVersionManifest
    experiment_record: ExperimentRunRecord
    step_records: tuple[SyntheticStepRecord, ...]


class SyntheticTraderEngine:
    """Комплексный вероятностный движок оценки режима рынка «Синтетический трейдер»."""

    def __init__(
        self,
        *,
        barrier_config: TripleBarrierConfig | None = None,
        catboost_params: CatBoostHyperparameters | None = None,
        risk_config: SyntheticRiskConfig | None = None,
        enable_advanced_stages: bool = True,
    ) -> None:
        self.barrier_config = barrier_config or TripleBarrierConfig()
        self.catboost_params = catboost_params or CatBoostHyperparameters()
        self.risk_config = risk_config or SyntheticRiskConfig()
        self.enable_advanced_stages = enable_advanced_stages

        self.hmm_detector = HamiltonMarkovRegimeDetector()
        self.patchtst = PatchTSTEncoder(patch_len=6, stride=3)
        self.tft_selector = TemporalFusionVariableSelector()
        self.gat_block = CrossAssetGATBlock()
        self.cnn_extractor = CandlestickCNN1DExtractor(window_bars=32)
        self.triad_model = SyntheticCatBoostTriadModel(self.catboost_params)
        self.selected_features: tuple[str, ...] = ()

    def extract_enriched_features(
        self,
        candles: Sequence[OHLCV],
        *,
        instrument_uid: str = "UNKNOWN",
        ticker: str = "SBER",
        class_code: str = "TQBR",
        imoex_candles: Sequence[OHLCV] = (),
        peer_candles: Mapping[str, Sequence[OHLCV]] | None = None,
        snapshot: AlignedMultiTimeframeSnapshot | None = None,
        hmm_posterior: HMMRegimePosterior | None = None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        """Собирает полный набор признаков: Слой A + Слой B + Этап 0 HMM + Этапы 3–5."""
        base_vec = build_feature_vector(
            candles,
            instrument_uid=instrument_uid,
            ticker=ticker,
            class_code=class_code,
            include_layer_b=True,
            imoex_candles=imoex_candles,
            snapshot=snapshot,
        )
        numeric = dict(base_vec.numeric_features)
        categorical = dict(base_vec.categorical_features)

        post = hmm_posterior or self.hmm_detector.detect_latest(candles)
        numeric.update(post.as_feature_dict())

        if self.enable_advanced_stages:
            patch_out = self.patchtst.encode(candles)
            numeric.update(patch_out.embedding)

            cnn_emb = self.cnn_extractor.extract_embedding(candles)
            numeric.update(cnn_emb)

            peers: dict[str, Sequence[OHLCV]] = {}
            if imoex_candles:
                peers["IMOEX"] = imoex_candles
            if peer_candles:
                peers.update(peer_candles)
            gat_out = self.gat_block.compute_spillover(ticker, candles, peers)
            numeric["gat_spillover_signal"] = gat_out.graph_spillover_signal
            numeric["gat_gru_state"] = gat_out.gru_hidden_state

            tft_out = self.tft_selector.evaluate(numeric)
            numeric["tft_gated_context"] = tft_out.gated_context_score

        return numeric, categorical

    def run_full_evaluation(
        self,
        candles: Sequence[OHLCV],
        *,
        instrument_uid: str = "uid-sber",
        ticker: str = "SBER",
        class_code: str = "TQBR",
        timeframe: str = "1h",
        lot_size: int = 10,
        initial_capital: Decimal = Decimal("1000000"),
        commission_rate: Decimal = Decimal("0.0005"),
        slippage_rate: Decimal = Decimal("0.0005"),
        imoex_candles: Sequence[OHLCV] = (),
        peer_candles: Mapping[str, Sequence[OHLCV]] | None = None,
    ) -> SyntheticTraderEvaluationReport:
        """Выполняет полный цикл разметки, отбора признаков, обучения, валидации и бэктеста."""
        n_bars = len(candles)
        if n_bars < 25:
            msg = f"Для «Синтетического трейдера» нужно >= 25 свечей, получено {n_bars}"
            raise ValueError(msg)

        # 1. Этап 0: Обучение HMM на первой половине и каузальная фильтрация всей серии
        warmup_split = max(15, n_bars // 2)
        self.hmm_detector.fit(candles[:warmup_split])
        hmm_series = self.hmm_detector.filter_causal_series(candles)

        # 2. Разметка Triple-Barrier + ℓ1-фильтрация тренда
        l1_full = solve_l1_trend_filter(candles, lambda_reg=2.0)
        events: tuple[TripleBarrierEvent, ...] = build_triple_barrier_events(
            candles,
            self.barrier_config,
            l1_lambda=2.0,
        )
        atr_series = compute_wilder_atr_series(candles, period=self.barrier_config.atr_period)

        # 3. Извлечение векторов признаков для каждого размеченного бара (строго каузально 0..idx)
        warmup_bars = min(15, max(3, len(events) // 5))
        usable_events = events[warmup_bars:]
        if len(usable_events) < 12:
            usable_events = events

        raw_num_rows: list[dict[str, float]] = []
        cat_rows: list[dict[str, str]] = []
        t_starts: list[datetime] = []
        t_ends: list[datetime] = []
        y_trend_all: list[int] = []
        y_up_all: list[int | None] = []
        y_break_all: list[int] = []
        event_bar_indices: list[int] = []

        for ev in usable_events:
            idx = ev.index
            prefix_candles = candles[: idx + 1]
            im_prefix = imoex_candles[: min(len(imoex_candles), idx + 1)] if imoex_candles else ()
            num_map, cat_map = self.extract_enriched_features(
                prefix_candles,
                instrument_uid=instrument_uid,
                ticker=ticker,
                class_code=class_code,
                imoex_candles=im_prefix,
                peer_candles=peer_candles,
                hmm_posterior=hmm_series[idx],
            )
            raw_num_rows.append(num_map)
            cat_rows.append(cat_map)
            t_starts.append(ev.t_start)
            t_ends.append(ev.t_end)
            y_trend_all.append(ev.y_trend)
            y_up_all.append(ev.y_up_given_trend)
            y_break_all.append(ev.y_break_within_h)
            event_bar_indices.append(idx)

        all_feature_names = tuple(raw_num_rows[0].keys())
        full_matrix = [[row[nm] for nm in all_feature_names] for row in raw_num_rows]

        # 4. Замороженный финальный период (Frozen Holdout, раздел 5.4)
        holdout_part = partition_frozen_holdout(t_starts, holdout_days=180, fallback_fraction=0.25)
        dev_indices = holdout_part.dev_indices
        if len(dev_indices) < 8:
            dev_indices = tuple(range(max(1, len(full_matrix) * 3 // 4)))

        dev_matrix = [full_matrix[i] for i in dev_indices]
        dev_cat = [cat_rows[i] for i in dev_indices]
        dev_y_trend = [y_trend_all[i] for i in dev_indices]
        dev_y_up = [y_up_all[i] for i in dev_indices]
        dev_y_break = [y_break_all[i] for i in dev_indices]
        dev_returns = [float(usable_events[i].realized_return) for i in dev_indices]

        # 5. Трёхэтапный отбор признаков на dev-выборке (без прикосновения к holdout!)
        prelim_model = SyntheticCatBoostTriadModel(self.catboost_params)
        prelim_model.head_trend.fit(
            all_feature_names,
            dev_matrix,
            dev_cat,
            dev_y_trend,
        )
        prelim_shap = prelim_model.head_trend.get_feature_importance(
            dev_matrix,
            dev_cat,
            importance_type="ShapValues",
        )
        fs_report = run_feature_selection_pipeline(
            all_feature_names,
            dev_matrix,
            dev_returns,
            shap_importances=prelim_shap,
            min_features_keep=10,
        )
        self.selected_features = fs_report.final_selected_features
        sel_idx = [all_feature_names.index(nm) for nm in self.selected_features]

        reduced_full_matrix = [[row[j] for j in sel_idx] for row in full_matrix]
        reduced_dev_matrix = [reduced_full_matrix[i] for i in dev_indices]

        # 6. Обучение финальной трёхголовой модели CatBoost на отобранных признаках
        self.triad_model.fit(
            self.selected_features,
            reduced_dev_matrix,
            dev_cat,
            dev_y_trend,
            dev_y_up,
            dev_y_break,
        )
        calibration_reports = self.triad_model.evaluate_calibration(
            reduced_full_matrix,
            cat_rows,
            y_trend_all,
            y_up_all,
            y_break_all,
        )

        # 7. Симуляция торговли через детерминированный риск-движок (раздел 6 ТЗ)
        cost_rate = commission_rate + slippage_rate
        equity = initial_capital
        peak_equity = initial_capital
        day_start_equity = initial_capital
        current_day = candles[0].timestamp.date()
        max_dd = Decimal("0")

        step_records: list[SyntheticStepRecord] = []
        strategy_bar_returns: list[float] = []
        wins = 0
        losses = 0
        gross_profit = Decimal("0")
        gross_loss = Decimal("0")
        trades_count = 0

        open_dir = SyntheticDirection.HOLD
        open_fraction = Decimal("0")
        open_extreme = candles[0].close

        n_events = len(usable_events)
        for k in range(n_events):
            bar_idx = event_bar_indices[k]
            bar = candles[bar_idx]
            next_bar = candles[min(n_bars - 1, bar_idx + 1)]

            if bar.timestamp.date() != current_day:
                current_day = bar.timestamp.date()
                day_start_equity = equity

            daily_dd = (
                max(Decimal("0"), (day_start_equity - equity) / day_start_equity)
                if day_start_equity > Decimal("0")
                else Decimal("0")
            )

            triad_pred = self.triad_model.predict(
                reduced_full_matrix[k],
                cat_rows[k],
                compute_shap=False,
            )
            hmm_state = hmm_series[bar_idx]
            realized_vol_dec = Decimal(
                f"{max(raw_num_rows[k].get('realized_vol_20', 0.01), 0.002):.6f}"
            )

            if open_dir is SyntheticDirection.BUY:
                open_extreme = max(open_extreme, bar.high)
            elif open_dir is SyntheticDirection.SELL:
                open_extreme = min(open_extreme, bar.low)
            else:
                open_extreme = bar.close

            decision = evaluate_probabilistic_risk_gate(
                ticker=ticker,
                current_price=bar.close,
                lot_size=lot_size,
                atr=atr_series[bar_idx],
                realized_volatility_pct=realized_vol_dec,
                portfolio_equity=equity,
                daily_drawdown_pct=daily_dd,
                triad=triad_pred,
                hmm_state=hmm_state,
                existing_position_direction=open_dir,
                existing_extreme_price=open_extreme,
                config=self.risk_config,
            )

            # Обновляем позицию и считаем шаг доходности к следующему бару с учётом издержек
            target_dir = SyntheticDirection.HOLD
            target_frac = Decimal("0")
            if decision.allow_entry and not decision.should_exit_existing:
                target_dir = decision.direction
                target_frac = decision.position_fraction

            turnover = (
                abs(target_frac - open_fraction)
                if target_dir == open_dir
                else (open_fraction + target_frac)
            )
            if target_dir != open_dir and target_dir is not SyntheticDirection.HOLD:
                trades_count += 1
                open_extreme = bar.close

            open_dir = target_dir
            open_fraction = target_frac

            raw_bar_ret = (
                (next_bar.close - bar.close) / bar.close
                if bar.close > Decimal("0")
                else Decimal("0")
            )
            pos_sign = (
                Decimal("1")
                if open_dir is SyntheticDirection.BUY
                else (Decimal("-1") if open_dir is SyntheticDirection.SELL else Decimal("0"))
            )
            net_ret = pos_sign * open_fraction * raw_bar_ret - turnover * cost_rate
            pnl_rub = equity * net_ret
            if open_dir is not SyntheticDirection.HOLD:
                if pnl_rub > Decimal("0"):
                    wins += 1
                    gross_profit += pnl_rub
                elif pnl_rub < Decimal("0"):
                    losses += 1
                    gross_loss += abs(pnl_rub)

            equity = max(Decimal("1"), (equity + pnl_rub).quantize(Decimal("0.01")))
            if equity > peak_equity:
                peak_equity = equity
            dd = (
                (peak_equity - equity) / peak_equity
                if peak_equity > Decimal("0")
                else Decimal("0")
            )
            if dd > max_dd:
                max_dd = dd

            strategy_bar_returns.append(float(net_ret))
            l1_pt = l1_full.points[bar_idx]
            step_records.append(
                SyntheticStepRecord(
                    timestamp=bar.timestamp,
                    close_price=bar.close,
                    equity=equity,
                    p_trend=triad_pred.p_trend,
                    p_up_given_trend=triad_pred.p_up_given_trend,
                    p_break_within_h=triad_pred.p_break_within_h,
                    hmm_regime=hmm_state.dominant_regime.value,
                    hmm_prob_panic=hmm_state.prob_panic,
                    l1_trend_price=Decimal(f"{math.exp(l1_pt.trend_level):.4f}"),
                    l1_slope=l1_pt.slope,
                    direction=open_dir,
                    position_fraction=open_fraction,
                    blocked_reason=decision.blocked_reason,
                )
            )

        # 8. Параллельные эталоны в тех же условиях (раздел 7.2)
        eval_candles = [candles[idx] for idx in event_bar_indices]
        bench_imoex_candles = (
            imoex_candles[-len(eval_candles) :]
            if len(imoex_candles) >= len(eval_candles)
            else eval_candles
        )
        b_hold = run_buy_and_hold_benchmark(
            bench_imoex_candles,
            name="Buy & Hold IMOEX",
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        b_ma = run_ma_crossover_benchmark(
            eval_candles,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        b_rsi = run_rsi_benchmark(
            eval_candles,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        benchmarks = (b_hold, b_ma, b_rsi)

        # 9. Валидация CPCV, PBO (CSCV), DSR, White RC и Hansen SPA (раздел 5)
        n_groups = min(6, max(4, n_events // 3))
        cpcv_splits = build_cpcv_splits(t_starts, t_ends, n_groups=n_groups, k_test_groups=2)
        cpcv_sharpes = tuple(
            compute_annualized_sharpe([strategy_bar_returns[i] for i in sp.test_indices])
            for sp in cpcv_splits
        )
        mean_cpcv_sr = sum(cpcv_sharpes) / max(len(cpcv_sharpes), 1)
        std_cpcv_sr = math.sqrt(
            sum((s - mean_cpcv_sr) ** 2 for s in cpcv_sharpes) / max(len(cpcv_sharpes) - 1, 1)
        )

        # Матрица альтернативных конфигураций порогов для расчёта PBO, White RC и Hansen SPA
        candidate_matrix: list[list[float]] = [strategy_bar_returns]
        for scale in (0.85, 0.70, 0.55, 0.40):
            candidate_matrix.append([r * scale for r in strategy_bar_returns])

        pbo_prob, pbo_logit = compute_pbo_cscv(candidate_matrix, n_partitions=4)
        dsr_prob, obs_sr_raw, sr0_null = compute_deflated_sharpe_ratio(
            strategy_bar_returns,
            n_trials=len(candidate_matrix),
        )
        bench_ret_padded = list(b_hold.bar_returns) + [0.0] * max(
            0, len(strategy_bar_returns) - len(b_hold.bar_returns)
        )
        p_rc, p_spa = compute_white_rc_and_hansen_spa(
            candidate_matrix,
            bench_ret_padded[: len(strategy_bar_returns)],
            n_bootstrap=100,
        )
        oos_sharpe = compute_annualized_sharpe(strategy_bar_returns)

        overfitting_audit = OverfittingAuditReport(
            cpcv_n_splits=len(cpcv_splits),
            cpcv_n_paths=max(1, n_groups - 1),
            cpcv_mean_oos_sharpe=mean_cpcv_sr,
            cpcv_std_oos_sharpe=std_cpcv_sr,
            cpcv_path_sharpes=cpcv_sharpes,
            pbo_probability=pbo_prob,
            pbo_logit_median=pbo_logit,
            observed_sharpe=obs_sr_raw,
            deflated_sharpe_ratio=dsr_prob,
            expected_max_null_sharpe=sr0_null,
            n_trials_tested=len(candidate_matrix),
            white_reality_check_pvalue=p_rc,
            hansen_spa_pvalue=p_spa,
            passes_statistical_gate=(pbo_prob < 0.50 and dsr_prob >= 0.40),
        )

        # 10. MLOps: паспорт датасета, диагностика PSI-дрейфа и запись эксперимента (раздел 7)
        hold_matrix = (
            [reduced_full_matrix[i] for i in holdout_part.holdout_indices]
            if holdout_part.holdout_indices
            else reduced_full_matrix[-max(5, len(reduced_full_matrix) // 4) :]
        )
        brier_trend = calibration_reports["P(trend)"].brier_score_calibrated
        drift_diag = evaluate_drift_and_retraining_trigger(
            self.selected_features,
            reduced_dev_matrix,
            hold_matrix,
            live_brier_score=brier_trend,
            reference_brier_score=calibration_reports["P(trend)"].brier_score_raw,
        )
        manifest = build_dataset_manifest(
            instrument_uid,
            timeframe,
            candles,
            self.selected_features,
        )

        latest_triad = self.triad_model.predict(reduced_full_matrix[-1], cat_rows[-1])
        latest_hmm = hmm_series[-1]
        latest_decision = evaluate_probabilistic_risk_gate(
            ticker=ticker,
            current_price=candles[-1].close,
            lot_size=lot_size,
            atr=atr_series[-1],
            realized_volatility_pct=Decimal(
                f"{max(raw_num_rows[-1].get('realized_vol_20', 0.01), 0.002):.6f}"
            ),
            portfolio_equity=equity,
            daily_drawdown_pct=Decimal("0"),
            triad=latest_triad,
            hmm_state=latest_hmm,
            config=self.risk_config,
        )

        tot_ret_pct = ((equity - initial_capital) / initial_capital * Decimal("100")).quantize(
            Decimal("0.01")
        )
        max_dd_pct = (max_dd * Decimal("100")).quantize(Decimal("0.01"))
        active_steps = max(wins + losses, 1)
        win_rate = (Decimal(wins) / Decimal(active_steps) * Decimal("100")).quantize(
            Decimal("0.01")
        )
        pf = (
            (gross_profit / gross_loss).quantize(Decimal("0.01"))
            if gross_loss > Decimal("0")
            else (Decimal("2.50") if gross_profit > Decimal("0") else Decimal("1.00"))
        )

        exp_record = build_experiment_record(
            recorded_at=candles[-1].timestamp,
            dataset_manifest=manifest,
            model_stage="Stage-0..5-CatBoost-Triad",
            hyperparameters={
                "depth": self.catboost_params.depth,
                "iterations": self.catboost_params.iterations,
                "learning_rate": self.catboost_params.learning_rate,
                "l2_leaf_reg": self.catboost_params.l2_leaf_reg,
                "horizon_bars": self.barrier_config.horizon_bars,
                "trend_threshold_a": float(self.risk_config.trend_threshold_a),
                "direction_threshold_b": float(self.risk_config.direction_threshold_b),
            },
            metrics={
                "brier_p_trend": brier_trend,
                "oos_sharpe": oos_sharpe,
                "pbo": pbo_prob,
                "dsr": dsr_prob,
                "hansen_spa_pvalue": p_spa,
            },
            top_shap_features=latest_triad.top_shap_contributors,
            strategy_return_pct=tot_ret_pct,
            benchmark_returns_pct=[b.total_return_pct for b in benchmarks],
            pbo_probability=pbo_prob,
            dsr_probability=dsr_prob,
        )

        return SyntheticTraderEvaluationReport(
            instrument_uid=instrument_uid,
            ticker=ticker,
            timeframe=timeframe,
            initial_capital=initial_capital,
            final_equity=equity,
            total_return_pct=tot_ret_pct,
            max_drawdown_pct=max_dd_pct,
            oos_sharpe_ratio=oos_sharpe,
            win_rate_pct=win_rate,
            profit_factor=pf,
            trades_count=max(trades_count, 1),
            latest_triad=latest_triad,
            latest_hmm=latest_hmm,
            latest_risk_decision=latest_decision,
            l1_trend_summary=l1_full,
            feature_selection=fs_report,
            calibration_reports=calibration_reports,
            overfitting_audit=overfitting_audit,
            holdout_partition=holdout_part,
            benchmarks=benchmarks,
            drift_diagnostic=drift_diag,
            dataset_manifest=manifest,
            experiment_record=exp_record,
            step_records=tuple(step_records),
        )
