"""Юнит-тесты подсистемы «Синтетический трейдер» (разделы 0–7 ТЗ)."""

from __future__ import annotations

import io
import math
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from adapters.driven.tbank.mappers import (
    CANDLE_INTERVAL_MAX_WINDOW,
    SYNTHETIC_INTERVAL_TO_API_ENUM,
    indicative_to_domain,
)
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV
from core.synthetic import (
    CatBoostHyperparameters,
    DriftStatus,
    HamiltonMarkovRegimeDetector,
    HMMRegimePosterior,
    PointInTimeFeatureStore,
    ProbabilisticTriadPrediction,
    ProbabilityCalibrator,
    SyntheticCatBoostTriadModel,
    SyntheticDirection,
    SyntheticMarketRegime,
    SyntheticRiskConfig,
    SyntheticTimeframe,
    SyntheticTraderEngine,
    TripleBarrierConfig,
    bar_close_timestamp,
    build_cpcv_splits,
    build_dataset_manifest,
    build_feature_vector,
    build_purged_kfold_splits,
    build_triple_barrier_events,
    compute_brier_score,
    compute_deflated_sharpe_ratio,
    compute_feature_psi,
    compute_pbo_cscv,
    compute_white_rc_and_hansen_spa,
    evaluate_drift_and_retraining_trigger,
    evaluate_probabilistic_risk_gate,
    extract_layer_a_features,
    is_bar_closed_at,
    partition_frozen_holdout,
    resample_candles,
    run_buy_and_hold_benchmark,
    run_feature_selection_pipeline,
    run_ma_crossover_benchmark,
    run_rsi_benchmark,
    solve_l1_trend_filter,
    verify_no_lookahead_leakage,
)


def _sample_candles(count: int = 72, *, tf: Timeframe = Timeframe.H1) -> tuple[OHLCV, ...]:
    start = datetime(2025, 3, 1, 7, 0, tzinfo=UTC)
    step = timedelta(hours=1) if tf is Timeframe.H1 else timedelta(minutes=1)
    bars: list[OHLCV] = []
    price = 260.0
    for i in range(count):
        drift = 1.2 * math.sin(i / 5.0) + (0.45 if i < count // 2 else -0.35)
        nxt = max(50.0, price + drift)
        hi = max(price, nxt) + 0.9
        lo = min(price, nxt) - 0.8
        bars.append(
            OHLCV(
                open=Decimal(f"{price:.2f}"),
                high=Decimal(f"{hi:.2f}"),
                low=Decimal(f"{lo:.2f}"),
                close=Decimal(f"{nxt:.2f}"),
                volume=12_000 + (i % 9) * 1_800,
                timestamp=start + step * i,
                timeframe=tf,
            )
        )
        price = nxt
    return tuple(bars)


def test_point_in_time_feature_store_prevents_lookahead_leakage() -> None:
    """Раздел 1.3 ТЗ: старшие фреймы приводятся к младшим строго по закрытым барам."""
    candles_1h = _sample_candles(50, tf=Timeframe.H1)
    candles_1d = resample_candles(candles_1h, SyntheticTimeframe.D1, domain_timeframe=Timeframe.D1)

    store = PointInTimeFeatureStore()
    store.put_candles("uid-sber", SyntheticTimeframe.H1, candles_1h)
    store.put_candles("uid-sber", SyntheticTimeframe.D1, candles_1d)

    # Свеча 10:00 (1h) ещё НЕ закрыта в 10:30, но закрыта в 11:00
    bar0 = candles_1h[0]
    assert not is_bar_closed_at(bar0, bar0.timestamp + timedelta(minutes=30), SyntheticTimeframe.H1)
    assert is_bar_closed_at(bar0, bar_close_timestamp(bar0, SyntheticTimeframe.H1))

    probes = [
        candles_1h[22].timestamp + timedelta(minutes=25),
        candles_1h[35].timestamp + timedelta(minutes=45),
    ]
    leak_free = verify_no_lookahead_leakage(
        store,
        "uid-sber",
        probes,
        lambda snap: build_feature_vector(
            snap.closed_bars_by_tf["1h"],
            snapshot=snap,
        ).to_decimal_map(),
        hierarchy=(SyntheticTimeframe.D1, SyntheticTimeframe.H1),
    )
    assert leak_free is True


def test_triple_barrier_and_l1_trend_filter() -> None:
    """Раздел 2.1 и 2.2 ТЗ: тройной барьер + ℓ1-фильтрация кусочно-линейного тренда."""
    candles = _sample_candles(64)
    l1_res = solve_l1_trend_filter(candles, lambda_reg=2.0)
    assert len(l1_res.points) == len(candles)
    assert l1_res.residual_std >= 0.0
    assert l1_res.dual_gap < 1.0

    events = build_triple_barrier_events(
        candles,
        TripleBarrierConfig(horizon_bars=8, k_tp=Decimal("1.5"), k_sl=Decimal("1.1")),
    )
    assert len(events) > 20
    for ev in events:
        assert ev.barrier_label in {-1, 0, 1}
        assert ev.y_trend in {0, 1}
        assert ev.y_break_within_h in {0, 1}
        assert ev.t_end >= ev.t_start
        if ev.barrier_label == 0:
            assert ev.y_up_given_trend is None
        else:
            assert ev.y_up_given_trend in {0, 1}


def test_layer_a_normalized_moving_averages_and_feature_selection() -> None:
    """Раздел 3 ТЗ: 13 опорных MA/EMA в относительном виде и 3-этапный отбор признаков."""
    candles = _sample_candles(60)
    layer_a = extract_layer_a_features(candles)
    sma_dist_keys = [k for k in layer_a if k.startswith("sma_") and k.endswith("_dist_pct")]
    ema_dist_keys = [k for k in layer_a if k.startswith("ema_") and k.endswith("_dist_pct")]
    # 6 SMA + 7 EMA = 13 опорных скользящих средних (строго в диапазоне 5..15 по ТЗ)
    assert len(sma_dist_keys) + len(ema_dist_keys) == 13

    # Строим матрицу с искусственно дублированным коллинеарным признаком (|r| == 1.0)
    rows: list[list[float]] = []
    targets: list[float] = []
    names = ["f_signal", "f_collinear_dup", "f_vol", "f_noise"]
    for i in range(40):
        sig = math.sin(i * 0.3)
        dup = sig * 1.0001 + 0.00001
        vol = math.cos(i * 0.2)
        noise = 0.001 * ((i % 3) - 1)
        rows.append([sig, dup, vol, noise])
        targets.append(sig * 0.8 + 0.2 * vol)

    fs_report = run_feature_selection_pipeline(
        names,
        rows,
        targets,
        shap_importances={"f_signal": 0.9, "f_collinear_dup": 0.4, "f_vol": 0.5, "f_noise": 0.01},
        max_collinearity=0.90,
        min_features_keep=2,
    )
    assert "f_signal" in fs_report.final_selected_features
    assert "f_collinear_dup" not in fs_report.final_selected_features
    assert len(fs_report.dropped_collinear_pairs) >= 1


def test_hmm_regime_detector_and_catboost_triad_shap_additivity() -> None:
    """Раздел 4 ТЗ: HMM Гамильтона, 3 головы CatBoost, калибровка и тождество TreeSHAP."""
    candles = _sample_candles(60)
    hmm = HamiltonMarkovRegimeDetector().fit(candles[:35])
    posteriors = hmm.filter_causal_series(candles)
    assert len(posteriors) == len(candles)
    last_post = posteriors[-1]
    assert abs((last_post.prob_trend + last_post.prob_chop + last_post.prob_panic) - 1.0) < 1e-5
    assert last_post.dominant_regime in {
        SyntheticMarketRegime.TREND,
        SyntheticMarketRegime.CHOP,
        SyntheticMarketRegime.PANIC,
    }

    # Проверяем тождество аддитивности Шепли: sum(phi) + base_margin == margin
    f_names = ("log_ret_1", "atr_14_pct", "rsi_14_norm", "ema_21_dist_pct")
    x_rows = [
        [math.sin(i * 0.2), 0.01 + 0.002 * (i % 4), math.cos(i * 0.3), 0.005 * (i % 5)]
        for i in range(36)
    ]
    c_rows = [
        {"ticker": "SBER", "class_code": "TQBR", "day_of_week": str(i % 5)}
        for i in range(36)
    ]
    y_trend = [1 if x_rows[i][0] > 0 else 0 for i in range(36)]
    y_up: list[int | None] = [1 if x_rows[i][2] > 0 else 0 for i in range(36)]
    y_brk = [1 if i % 6 == 0 else 0 for i in range(36)]

    model = SyntheticCatBoostTriadModel(
        CatBoostHyperparameters(depth=4, iterations=16, learning_rate=0.05)
    ).fit(f_names, x_rows, c_rows, y_trend, y_up, y_brk)

    shap_vals = model.head_trend.compute_shap_values_row(x_rows[-1], c_rows[-1])
    enc_row = model.head_trend.encode_row(x_rows[-1], c_rows[-1])
    margin = model.head_trend.predict_margin_row(enc_row)
    shap_sum = sum(shap_vals.values()) + model.head_trend.base_margin
    assert abs(shap_sum - margin) < 1e-6

    pred = model.predict(x_rows[-1], c_rows[-1])
    assert 0.0 < pred.p_trend < 1.0
    assert 0.0 < pred.p_up_given_trend < 1.0
    assert 0.0 < pred.p_break_within_h < 1.0


def test_calibration_purged_cv_cpcv_pbo_dsr_and_spa() -> None:
    """Разделы 4.1 и 5 ТЗ: калибровка PAVA, Purged K-Fold + Embargo, CPCV, PBO, DSR и SPA."""
    raw_probs = [0.05 if i % 2 == 0 else 0.95 for i in range(40)]
    targets = [0 if i % 2 == 0 else 1 for i in range(40)]
    cal = ProbabilityCalibrator(method="hybrid").fit(raw_probs, targets)
    report = cal.evaluate("P(trend)", raw_probs, targets)
    assert 0.0 <= report.brier_score_calibrated <= 0.25
    assert len(report.reliability_bins) == 10
    assert compute_brier_score([0.5, 0.5], [1, 0]) == 0.25

    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    t_starts = [t0 + timedelta(hours=i) for i in range(48)]
    t_ends = [ts + timedelta(hours=4) for ts in t_starts]

    pkf = build_purged_kfold_splits(t_starts, t_ends, n_splits=4, embargo_pct=0.05)
    assert len(pkf) == 4
    for sp in pkf:
        assert sp.purged_count >= 0
        # Ни одно обучающее наблюдение не должно пересекаться по [t_start, t_end] с тестом
        test_start = t_starts[sp.test_indices[0]]
        test_end = max(t_ends[idx] for idx in sp.test_indices)
        for tr_i in sp.train_indices:
            assert not (t_starts[tr_i] <= test_end and t_ends[tr_i] >= test_start)

    cpcv = build_cpcv_splits(t_starts, t_ends, n_groups=6, k_test_groups=2)
    assert len(cpcv) == 15  # C(6, 2) = 15

    holdout = partition_frozen_holdout(t_starts, holdout_days=10, fallback_fraction=0.25)
    assert len(holdout.dev_indices) + len(holdout.holdout_indices) == 48

    strat_mat = [
        [0.003 + 0.001 * math.sin(t * 0.2 + s) for t in range(48)]
        for s in range(5)
    ]
    bench = [0.0005 * math.sin(t * 0.2) for t in range(48)]
    pbo, _ = compute_pbo_cscv(strat_mat, n_partitions=4)
    dsr, _, _ = compute_deflated_sharpe_ratio(strat_mat[0], n_trials=5)
    p_rc, p_spa = compute_white_rc_and_hansen_spa(strat_mat, bench, n_bootstrap=50)
    assert 0.0 <= pbo <= 1.0
    assert 0.0 <= dsr <= 1.0
    assert 0.0 <= p_rc <= 1.0
    assert 0.0 <= p_spa <= 1.0


def test_risk_engine_rules_and_sector_correlation_guards() -> None:
    """Раздел 6 ТЗ: вход при P(trend) > a, сжатие по P(break), запреты PANIC и GAZP+ROSN."""
    triad_strong = ProbabilisticTriadPrediction(
        p_trend=0.75,
        p_up_given_trend=0.72,
        p_break_within_h=0.15,
        raw_p_trend=0.76,
        raw_p_up_given_trend=0.73,
        raw_p_break_within_h=0.16,
        expected_directional_edge=0.33,
        confidence_adjusted_edge=0.28,
        top_shap_contributors={"log_ret_5": 0.12},
    )
    hmm_trend = HMMRegimePosterior(
        prob_trend=0.80,
        prob_chop=0.15,
        prob_panic=0.05,
        dominant_regime=SyntheticMarketRegime.TREND,
        expected_duration_bars=7.5,
        is_trading_banned=False,
    )
    decision_ok = evaluate_probabilistic_risk_gate(
        ticker="SBER",
        current_price=Decimal("280"),
        lot_size=10,
        atr=Decimal("3.5"),
        realized_volatility_pct=Decimal("0.010"),
        portfolio_equity=Decimal("1000000"),
        daily_drawdown_pct=Decimal("0.005"),
        triad=triad_strong,
        hmm_state=hmm_trend,
        config=SyntheticRiskConfig(),
    )
    assert decision_ok.allow_entry is True
    assert decision_ok.direction is SyntheticDirection.BUY
    assert decision_ok.recommended_lots > 0

    # Защита от концентрации в одном секторе: GAZP при уже открытой ROSN блокируется
    decision_sector_blocked = evaluate_probabilistic_risk_gate(
        ticker="GAZP",
        current_price=Decimal("160"),
        lot_size=10,
        atr=Decimal("2.0"),
        realized_volatility_pct=Decimal("0.010"),
        portfolio_equity=Decimal("1000000"),
        daily_drawdown_pct=Decimal("0.005"),
        triad=triad_strong,
        hmm_state=hmm_trend,
        active_tickers=("ROSN",),
        config=SyntheticRiskConfig(max_positions_per_sector=1),
    )
    assert decision_sector_blocked.allow_entry is False
    assert decision_sector_blocked.blocked_reason is not None
    assert "SECTOR_CONCENTRATION_LIMIT" in decision_sector_blocked.blocked_reason

    # Запрет торговли при режиме PANIC
    hmm_panic = HMMRegimePosterior(
        prob_trend=0.10,
        prob_chop=0.20,
        prob_panic=0.70,
        dominant_regime=SyntheticMarketRegime.PANIC,
        expected_duration_bars=4.0,
        is_trading_banned=True,
    )
    decision_panic = evaluate_probabilistic_risk_gate(
        ticker="SBER",
        current_price=Decimal("280"),
        lot_size=10,
        atr=Decimal("3.5"),
        realized_volatility_pct=Decimal("0.025"),
        portfolio_equity=Decimal("1000000"),
        daily_drawdown_pct=Decimal("0.005"),
        triad=triad_strong,
        hmm_state=hmm_panic,
    )
    assert decision_panic.allow_entry is False
    assert decision_panic.blocked_reason == "HMM_PANIC_REGIME_BAN"


def test_mlops_psi_drift_benchmarks_and_tbank_archive_helpers() -> None:
    """Раздел 1 и 7 ТЗ: PSI-дрейф, бенчмарки, маппинг интервалов и парсер ZIP-архива."""
    candles = _sample_candles(60)
    b1 = run_buy_and_hold_benchmark(candles)
    b2 = run_ma_crossover_benchmark(candles)
    b3 = run_rsi_benchmark(candles)
    assert len(b1.equity_curve) == len(candles)
    assert len(b2.equity_curve) == len(candles)
    assert len(b3.equity_curve) == len(candles)

    ref_col = [0.01 * (i % 10) for i in range(50)]
    shifted_col = [0.50 + 0.01 * (i % 10) for i in range(50)]
    assert compute_feature_psi(ref_col, ref_col) < 0.01
    assert compute_feature_psi(ref_col, shifted_col) >= 0.20

    diag = evaluate_drift_and_retraining_trigger(
        ["f1"],
        [[v] for v in ref_col],
        [[v] for v in shifted_col],
    )
    assert diag.overall_status is DriftStatus.CRITICAL_RETRAIN
    assert diag.should_trigger_retraining is True

    manifest = build_dataset_manifest("uid-sber", "1h", candles, ["f1", "f2"])
    assert manifest.dataset_id.startswith("dvc-uid-sber-1h-")
    assert len(manifest.sha256_digest) == 64

    # Проверка расширенных интервалов T-Invest API и индикатива IMOEX
    for tf_key in ("5m", "15m", "1h", "4h", "1d"):
        assert tf_key in SYNTHETIC_INTERVAL_TO_API_ENUM
        assert tf_key in CANDLE_INTERVAL_MAX_WINDOW

    class _RawIndicative:
        uid = "uid-imoex"
        ticker = "IMOEX"
        class_code = "SPBXM"
        currency = "rub"

    imoex_inst = indicative_to_domain(_RawIndicative())
    assert imoex_inst.is_benchmark is True
    assert imoex_inst.ticker == "IMOEX"

    # Проверка распаковки ZIP-архива минутных свечей T-Invest history-data
    from adapters.driven.tbank.history_loader import parse_history_zip_bytes

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        csv_lines = (
            "uid-sber;2024-05-02T07:00:00Z;280.10;280.50;280.80;279.90;15000;\n"
            "uid-sber;2024-05-02T07:01:00Z;280.50;281.00;281.20;280.40;18200;\n"
        )
        zf.writestr("20240502.csv", csv_lines)
    parsed_1m = parse_history_zip_bytes(buf.getvalue())
    assert len(parsed_1m) == 2
    assert parsed_1m[0].close == Decimal("280.50")


def test_full_synthetic_trader_engine_end_to_end() -> None:
    """Полный сквозной прогон SyntheticTraderEngine и соревнование 6 алгоритмов."""
    from core.synthetic.engine import generate_reference_moex_series

    candles, imoex = generate_reference_moex_series(
        ticker="SBER",
        bar_count=120,
        scenario="cycle",
    )
    engine = SyntheticTraderEngine()
    report = engine.run_full_evaluation(
        candles,
        instrument_uid="uid-sber",
        ticker="SBER",
        imoex_candles=imoex,
    )
    assert report.ticker == "SBER"
    assert len(report.benchmarks) == 5
    assert report.trades_count >= 5
    assert len(report.completed_trades) == report.trades_count
    assert report.total_return_pct > Decimal("0")
    assert report.overfitting_audit.cpcv_n_splits > 0
    assert len(report.step_records) > 20
    assert report.human_summary != ""

