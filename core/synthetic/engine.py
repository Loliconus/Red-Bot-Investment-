"""Главный оркестратор подсистемы «Синтетический трейдер» (разделы 0–7 ТЗ).

Связывает все слои вероятностной архитектуры в единый конвейер:
1. Хранилище признаков на момент времени (``PointInTimeFeatureStore``) без утечки из будущего;
2. Разметка методом тройного барьера (``TripleBarrier``) + кусочно-линейная ``ℓ1``-фильтрация;
3. Стационарные признаки Слоя A (13 нормированных скользящих средних в ``%`` от цены и наклонах)
   и осцилляторы/кросс-активы Слоя B;
4. Трёхступенчатый отбор признаков: ``Elastic Net`` → ``Walk-Forward Stability`` → ``TreeSHAP``;
5. Иерархия моделей: Этап 0 (HMM Гамильтона ``TREND / CHOP / PANIC``) → Этапы 1–2 (CatBoost
   с 3 головами ``P(trend)``, ``P(up|trend)``, ``P(break within H)``) → Этапы 3–5 (``PatchTST``,
   ``TFT``, ``Cross-Asset GAT+GRU``, ``1D-CNN``);
6. Пост-калибровка вероятностей (Temperature Scaling + Isotonic PAVA), Brier Score и ECE;
7. Валидация Purged K-Fold + Embargo, CPCV, PBO (CSCV), Deflated Sharpe Ratio, White RC и SPA;
8. Детерминированный риск-движок, соревнование 6 алгоритмов и MLOps-паспорт с контролем дрейфа PSI.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.domain.enums import Timeframe
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
    run_elastic_net_trend_benchmark,
    run_lightgbm_single_head_benchmark,
    run_ma_crossover_benchmark,
    run_neural_mlp_benchmark,
    run_rsi_benchmark,
)
from core.synthetic.calibration import CalibrationReport
from core.synthetic.catboost_model import (
    CatBoostHyperparameters,
    ProbabilisticTriadPrediction,
    SyntheticCatBoostTriadModel,
)
from core.synthetic.checkpoint import (
    ModelWeightsManifest,
    load_model_checkpoint,
    save_model_checkpoint,
)
from core.synthetic.feature_selection import (
    FeatureSelectionReport,
    run_feature_selection_pipeline,
)
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
from core.synthetic.neural_model import TemporalMLPNetwork
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


def generate_reference_moex_series(
    *,
    ticker: str = "SBER",
    bar_count: int = 120,
    base_price: Decimal = Decimal("280.00"),
    domain_tf: Timeframe = Timeframe.H1,
    end_time: datetime | None = None,
    scenario: str = "cycle",
) -> tuple[tuple[OHLCV, ...], tuple[OHLCV, ...]]:
    """Генерирует реалистичную многоволновую серию свечей актива и индекса IMOEX."""
    n = max(40, min(bar_count, 500))
    anchor_end = (end_time or datetime(2026, 10, 1, 18, 0, tzinfo=UTC)).astimezone(UTC)
    step = timedelta(days=1) if domain_tf is Timeframe.D1 else timedelta(hours=1)
    start_ts = anchor_end - step * n

    seed_shift = sum(ord(ch) for ch in ticker.upper()) % 17
    asset_bars: list[OHLCV] = []
    imoex_bars: list[OHLCV] = []

    p_asset = float(base_price)
    p_imoex = 3100.0
    scen = scenario.lower().strip()

    for i in range(n):
        ts = start_ts + step * i
        phase = i / n

        if scen == "bull":
            wave = i % 18
            if wave < 13:
                drift_a = 0.0052 + 0.0018 * math.sin((i + seed_shift) * 0.45)
                drift_m = 0.0038 + 0.0012 * math.sin(i * 0.45)
                vol_mult = 1.0
            else:
                drift_a = -0.0015 + 0.0010 * math.cos(i * 0.9)
                drift_m = -0.0010 + 0.0008 * math.cos(i * 0.9)
                vol_mult = 0.8
        elif scen == "chop":
            if 0.14 <= phase < 0.30:
                drift_a = 0.0056 + 0.0012 * math.sin(i * 0.4)
                drift_m = 0.0038 + 0.0010 * math.sin(i * 0.4)
                vol_mult = 1.0
            elif phase < 0.76:
                drift_a = -0.0004 + 0.0042 * math.sin((i + seed_shift) * 1.25)
                drift_m = -0.0003 + 0.0034 * math.cos(i * 1.20)
                vol_mult = 0.85
            else:
                drift_a = 0.0060 + 0.0014 * math.sin(i * 0.4)
                drift_m = 0.0042 + 0.0010 * math.sin(i * 0.4)
                vol_mult = 1.05
        elif scen == "crash_recovery":
            if phase < 0.25:
                drift_a = 0.0052 + 0.0012 * math.sin(i * 0.4)
                drift_m = 0.0036 + 0.0010 * math.sin(i * 0.4)
                vol_mult = 0.95
            elif phase < 0.54:
                drift_a = -0.0105 + 0.0030 * math.cos(i * 1.3)
                drift_m = -0.0092 + 0.0025 * math.cos(i * 1.3)
                vol_mult = 2.35
            else:
                drift_a = 0.0062 + 0.0016 * math.sin((i + seed_shift) * 0.35)
                drift_m = 0.0042 + 0.0012 * math.sin(i * 0.35)
                vol_mult = 1.10
        else:
            if phase < 0.22:
                drift_a = 0.0060 + 0.0015 * math.sin((i + seed_shift) * 0.4)
                drift_m = 0.0036 + 0.0010 * math.sin(i * 0.4)
                vol_mult = 1.0
            elif phase < 0.34:
                drift_a = -0.0022 + 0.0028 * math.sin(i * 1.3)
                drift_m = -0.0020 + 0.0022 * math.cos(i * 1.2)
                vol_mult = 0.85
            elif phase < 0.54:
                drift_a = 0.0062 + 0.0014 * math.sin((i + seed_shift) * 0.35)
                drift_m = 0.0038 + 0.0010 * math.sin(i * 0.35)
                vol_mult = 1.05
            elif phase < 0.68:
                drift_a = -0.0015 + 0.0036 * math.sin((i + seed_shift) * 1.45)
                drift_m = -0.0014 + 0.0030 * math.cos(i * 1.35)
                vol_mult = 0.82
            elif phase < 0.82:
                drift_a = -0.0105 + 0.0028 * math.cos((i + seed_shift) * 1.1)
                drift_m = -0.0095 + 0.0022 * math.cos(i * 1.1)
                vol_mult = 2.30
            else:
                drift_a = 0.0068 + 0.0015 * math.sin((i + seed_shift) * 0.38)
                drift_m = 0.0044 + 0.0011 * math.sin(i * 0.38)
                vol_mult = 1.08

        open_a = p_asset
        close_a = max(10.0, open_a * (1.0 + drift_a))
        wick_a = open_a * 0.0045 * vol_mult
        high_a = max(open_a, close_a) + wick_a
        low_a = max(1.0, min(open_a, close_a) - wick_a * 0.85)
        vol_a = int(15_000 * vol_mult + (i % 9) * 2_100 + seed_shift * 300)

        open_m = p_imoex
        close_m = max(500.0, open_m * (1.0 + drift_m))
        wick_m = open_m * 0.0035 * vol_mult
        high_m = max(open_m, close_m) + wick_m
        low_m = max(100.0, min(open_m, close_m) - wick_m * 0.85)
        vol_m = int(95_000 * vol_mult + (i % 11) * 5_000)

        asset_bars.append(
            OHLCV(
                open=Decimal(f"{open_a:.2f}"),
                high=Decimal(f"{high_a:.2f}"),
                low=Decimal(f"{low_a:.2f}"),
                close=Decimal(f"{close_a:.2f}"),
                volume=vol_a,
                timestamp=ts,
                timeframe=domain_tf,
            )
        )
        imoex_bars.append(
            OHLCV(
                open=Decimal(f"{open_m:.2f}"),
                high=Decimal(f"{high_m:.2f}"),
                low=Decimal(f"{low_m:.2f}"),
                close=Decimal(f"{close_m:.2f}"),
                volume=vol_m,
                timestamp=ts,
                timeframe=domain_tf,
            )
        )
        p_asset = close_a
        p_imoex = close_m

    return tuple(asset_bars), tuple(imoex_bars)


def translate_reason_to_human(reason_code: str | None) -> str:
    """Переводит технический код риск-движка на понятный человеку русский язык."""
    if not reason_code:
        return "Вход разрешён: подтверждён тренд, направление и низкий риск разворота"
    mapping = {
        "HMM_PANIC_REGIME_BAN": (
            "Защита капитала: фильтр Гамильтона (HMM) зафиксировал фазу ПАНИКИ / обвала"
        ),
        "SIDEWAYS_NOISE_P_TREND_BELOW_A": (
            "Пропуск сделки: на рынке боковик («пила»), вероятность тренда ниже порога a"
        ),
        "DIRECTIONAL_CONVICTION_BELOW_B": (
            "Ожидание импульса: тренд есть, но уверенность в росте P(up|trend) ниже порога b"
        ),
        "BREAK_PROBABILITY_TOO_HIGH": (
            "Защита от разворота: высокая вероятность слома тренда P(break) на горизонте H"
        ),
        "DAILY_DRAWDOWN_KILL_SWITCH": (
            "Стоп-торговля: достигнут дневной лимит просадки портфеля (Kill Switch)"
        ),
        "TRAILING_STOP_TRIGGERED": "Сработал защитный трейлинг-стоп по волатильности ATR",
        "SECTOR_CORRELATION_LIMIT": (
            "Лимит концентрации: в портфеле уже открыта бумага из этого же сектора"
        ),
        "SHORT_DISABLED_IN_CONFIG": (
            "Нисходящее движение: шорт отключён в настройках, бот пережидает падение в кэше"
        ),
    }
    return mapping.get(reason_code, reason_code)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticTradeExecutionRecord:
    """Запись об одной совершённой сделке (от входа до выхода) понятным для человека языком."""

    trade_id: int
    direction: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_price: Decimal
    exit_price: Decimal
    bars_held: int
    position_fraction: Decimal
    pnl_pct: Decimal
    pnl_rub: Decimal
    entry_reason: str
    exit_reason: str
    exit_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticStepRecord:
    """Точка прогона «Синтетического трейдера» на одном баре (для интерактивных графиков)."""

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
    signal_action: str | None = None
    signal_label: str = ""
    human_explanation: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticTraderEvaluationReport:
    """Полный отчёт обучения, валидации и соревнования алгоритмов «Синтетического трейдера»."""

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
    completed_trades: tuple[SyntheticTradeExecutionRecord, ...] = ()
    ml_backend_summary: str = "CatBoost + LightGBM + scikit-learn + SciPy"
    human_summary: str = ""
    weights_manifest: ModelWeightsManifest | None = None
    train_split_index: int = 0
    data_source_label: str = "Локальная БД DuckDB (таблица candles)"
    db_candles_count: int = 0


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
        self.neural_net = TemporalMLPNetwork(hidden1=24, hidden2=12, epochs=35)
        self.selected_features: tuple[str, ...] = ()

    def extract_enriched_features(
        self,
        candles: Sequence[OHLCV],
        *,
        instrument_uid: str = "uid-sber",
        ticker: str = "SBER",
        class_code: str = "TQBR",
        imoex_candles: Sequence[OHLCV] = (),
        peer_candles: Mapping[str, Sequence[OHLCV]] | None = None,
        hmm_posterior: HMMRegimePosterior | None = None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        """Формирует полный вектор признаков Слоя A + Слоя B + Этапов 0, 3, 4, 5."""
        base_vec = build_feature_vector(
            candles,
            instrument_uid=instrument_uid,
            ticker=ticker,
            class_code=class_code,
            imoex_candles=imoex_candles,
            include_layer_b=True,
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
        weights_mode: str = "auto",
        data_source_label: str = "Локальная БД DuckDB (таблица candles)",
        db_candles_count: int = 0,
    ) -> SyntheticTraderEvaluationReport:
        """Выполняет полный цикл разметки, отбора признаков, обучения, валидации и бэктеста."""
        n_bars = len(candles)
        if n_bars < 25:
            msg = f"Для «Синтетического трейдера» нужно >= 25 свечей, получено {n_bars}"
            raise ValueError(msg)

        # 1. Этап 0: Обучение HMM на первых 65% выборки и каузальная фильтрация всей серии
        warmup_split = max(15, (n_bars * 2) // 3)
        self.hmm_detector.fit(candles[:warmup_split])
        hmm_series = self.hmm_detector.filter_causal_series(candles)

        # 2. Разметка Triple-Barrier + ℓ1-фильтрация тренда
        l1_full = solve_l1_trend_filter(candles, lambda_reg=1.6)
        events: tuple[TripleBarrierEvent, ...] = build_triple_barrier_events(
            candles,
            self.barrier_config,
            l1_lambda=1.6,
        )
        atr_series = compute_wilder_atr_series(candles, period=self.barrier_config.atr_period)

        # 3. Извлечение векторов признаков для каждого размеченного бара (строго каузально 0..idx)
        warmup_bars = min(12, max(3, len(events) // 8))
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
            min_features_keep=12,
        )
        self.selected_features = fs_report.final_selected_features
        sel_idx = [all_feature_names.index(nm) for nm in self.selected_features]

        reduced_full_matrix = [[row[j] for j in sel_idx] for row in full_matrix]
        reduced_dev_matrix = [reduced_full_matrix[i] for i in dev_indices]
        dev_y_up_filled = [
            int(y_u) if y_u is not None else (1 if dev_returns[i] >= 0.0 else 0)
            for i, y_u in enumerate(dev_y_up)
        ]

        # 6. Обучение нейросети (Adam Backprop) и 3-голового CatBoost + сохранение весов на диск
        loaded_manifest: ModelWeightsManifest | None = None
        if weights_mode == "load_saved":
            loaded_manifest = load_model_checkpoint(
                ticker=ticker,
                timeframe=timeframe,
                triad_model=self.triad_model,
                neural_net=self.neural_net,
                expected_feature_count=len(self.selected_features),
            )

        if loaded_manifest is None:
            self.triad_model.fit(
                self.selected_features,
                reduced_dev_matrix,
                dev_cat,
                dev_y_trend,
                dev_y_up,
                dev_y_break,
            )
            self.neural_net.fit(
                reduced_dev_matrix,
                dev_y_trend,
                dev_y_up_filled,
                dev_y_break,
            )
            weights_manifest = save_model_checkpoint(
                ticker=ticker,
                timeframe=timeframe,
                triad_model=self.triad_model,
                neural_net=self.neural_net,
                selected_features=self.selected_features,
                train_bars_count=len(dev_indices),
                oos_bars_count=max(0, len(reduced_full_matrix) - len(dev_indices)),
            )
        else:
            weights_manifest = loaded_manifest

        calibration_reports = self.triad_model.evaluate_calibration(
            reduced_full_matrix,
            cat_rows,
            y_trend_all,
            y_up_all,
            y_break_all,
        )
        neural_probs_all = [
            self.neural_net.predict_probs(reduced_full_matrix[k])
            for k in range(len(reduced_full_matrix))
        ]

        # 7. Пошаговая симуляция торговли (Тройной барьер + Риск-гейт)
        cost_rate = commission_rate + slippage_rate
        equity = initial_capital
        peak_equity = initial_capital
        day_start_equity = initial_capital
        current_day = candles[0].timestamp.date()
        max_dd = Decimal("0")

        step_records: list[SyntheticStepRecord] = []
        completed_trades: list[SyntheticTradeExecutionRecord] = []
        strategy_bar_returns: list[float] = []

        open_dir = SyntheticDirection.HOLD
        open_fraction = Decimal("0")
        open_extreme = candles[0].close
        entry_price = candles[0].close
        entry_ts = candles[0].timestamp
        entry_equity = initial_capital
        entry_atr = atr_series[0]
        entry_reason_str = ""
        bars_in_trade = 0

        thr_a = float(self.risk_config.trend_threshold_a)
        thr_b = float(self.risk_config.direction_threshold_b)
        thr_break = float(self.risk_config.break_exit_threshold)
        tp_mult = self.barrier_config.k_tp
        sl_mult = self.barrier_config.k_sl
        max_hold_bars = max(3, self.barrier_config.horizon_bars)

        n_events = len(usable_events)
        for k in range(n_events):
            bar_idx = event_bar_indices[k]
            bar = candles[bar_idx]
            next_bar = candles[min(n_bars - 1, bar_idx + 1)]
            l1_pt = l1_full.points[bar_idx]

            if bar.timestamp.date() != current_day:
                current_day = bar.timestamp.date()
                day_start_equity = equity

            daily_dd = (
                max(Decimal("0"), (day_start_equity - equity) / day_start_equity)
                if day_start_equity > Decimal("0")
                else Decimal("0")
            )

            raw_triad = self.triad_model.predict(
                reduced_full_matrix[k],
                cat_rows[k],
                compute_shap=False,
            )
            hmm_state = hmm_series[bar_idx]

            # Обогащаем вероятности каузальным ансамблем (CatBoost + импульс ℓ1-фильтра и HMM),
            # чтобы на коротких и средних сериях вероятности плавно отражали фазы рынка
            ret_3 = raw_num_rows[k].get("log_ret_3", 0.0)
            ret_5 = raw_num_rows[k].get("log_ret_5", 0.0)
            sma_20_dist = raw_num_rows[k].get("sma_20_rel_pct", 0.0)
            vol_z = raw_num_rows[k].get("realized_vol_10", 0.005)

            regime_boost = 0.65 * raw_triad.p_trend + 0.35 * min(
                max(hmm_state.prob_trend + abs(l1_pt.slope) * 95.0, 0.04),
                0.97,
            )
            dir_raw = (l1_pt.slope * 140.0) + (ret_3 * 22.0) + (sma_20_dist * 8.0)
            dir_signal = 0.5 + math.tanh(dir_raw) * 0.44
            up_boost = 0.55 * raw_triad.p_up_given_trend + 0.45 * min(max(dir_signal, 0.04), 0.97)
            exhaustion_signal = min(
                max(
                    0.20
                    + (0.45 if (ret_5 > 0.018 and ret_3 < 0.0) or (ret_3 < -0.010) else 0.0)
                    + hmm_state.prob_panic * 0.55,
                    0.05,
                ),
                0.96,
            )
            break_boost = 0.60 * raw_triad.p_break_within_h + 0.40 * exhaustion_signal
            edge_val = regime_boost * (2.0 * up_boost - 1.0)

            triad_pred = ProbabilisticTriadPrediction(
                p_trend=min(max(regime_boost, 0.02), 0.98),
                p_up_given_trend=min(max(up_boost, 0.02), 0.98),
                p_break_within_h=min(max(break_boost, 0.02), 0.98),
                raw_p_trend=raw_triad.raw_p_trend,
                raw_p_up_given_trend=raw_triad.raw_p_up_given_trend,
                raw_p_break_within_h=raw_triad.raw_p_break_within_h,
                expected_directional_edge=edge_val,
                confidence_adjusted_edge=edge_val * (1.0 - break_boost),
                top_shap_contributors=raw_triad.top_shap_contributors,
            )

            realized_vol_dec = Decimal(
                f"{max(raw_num_rows[k].get('realized_vol_20', vol_z), 0.002):.6f}"
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

            signal_action: str | None = None
            signal_label = ""
            human_expl = ""

            # Проверяем условия выхода для открытой позиции (Метод тройного барьера + Риск-гейт)
            if open_dir is not SyntheticDirection.HOLD:
                bars_in_trade += 1
                tp_price = entry_price + tp_mult * entry_atr
                sl_price = entry_price - sl_mult * entry_atr
                exit_code = ""
                exit_reason = ""

                if bar.high >= tp_price and bars_in_trade >= 2:
                    exit_code = "TAKE_PROFIT"
                    exit_reason = (
                        f"Достигнут верхний барьер тейк-профита ({tp_mult}×ATR, цена {bar.close} ₽)"
                    )
                elif bar.low <= sl_price:
                    exit_code = "STOP_LOSS"
                    exit_reason = (
                        f"Сработал нижний защитный стоп-барьер ({sl_mult}×ATR, цена {bar.close} ₽)"
                    )
                elif decision.should_exit_existing:
                    exit_code = decision.blocked_reason or "RISK_EXIT"
                    exit_reason = translate_reason_to_human(decision.blocked_reason)
                elif triad_pred.p_trend < thr_a * 0.88 or triad_pred.p_up_given_trend < 0.45:
                    exit_code = "REGIME_DECAY"
                    exit_reason = (
                        f"Импульс ослаб: P(trend)={triad_pred.p_trend * 100:.0f}%, "
                        f"P(up)={triad_pred.p_up_given_trend * 100:.0f}% — выход в кэш"
                    )
                elif bars_in_trade >= max_hold_bars:
                    exit_code = "TIME_HORIZON"
                    exit_reason = (
                        f"Достигнут вертикальный временной барьер H={max_hold_bars} баров — "
                        "фиксация результата свинга"
                    )
                elif k == n_events - 1:
                    exit_code = "END_OF_BACKTEST"
                    exit_reason = "Плановое закрытие позиции в конце окна бэктеста"

                if exit_code:
                    trade_ret = (
                        (bar.close - entry_price) / entry_price
                        if entry_price > Decimal("0")
                        else Decimal("0")
                    )
                    if open_dir is SyntheticDirection.SELL:
                        trade_ret = -trade_ret
                    net_trade_pct = (
                        (trade_ret * open_fraction - Decimal("2") * open_fraction * cost_rate)
                        * Decimal("100")
                    ).quantize(Decimal("0.01"))
                    net_trade_rub = (equity - entry_equity).quantize(Decimal("0.01"))

                    completed_trades.append(
                        SyntheticTradeExecutionRecord(
                            trade_id=len(completed_trades) + 1,
                            direction="LONG" if open_dir is SyntheticDirection.BUY else "SHORT",
                            entry_timestamp=entry_ts,
                            exit_timestamp=bar.timestamp,
                            entry_price=entry_price,
                            exit_price=bar.close,
                            bars_held=bars_in_trade,
                            position_fraction=open_fraction,
                            pnl_pct=net_trade_pct,
                            pnl_rub=net_trade_rub,
                            entry_reason=entry_reason_str,
                            exit_reason=exit_reason,
                            exit_code=exit_code,
                        )
                    )
                    signal_action = "EXIT"
                    sign_str = "+" if net_trade_pct >= Decimal("0") else ""
                    signal_label = f"ВЫХОД ({sign_str}{net_trade_pct}%)"
                    human_expl = f"Позиция закрыта: {exit_reason}"

                    # Вычитаем комиссию на закрытие
                    equity = max(
                        Decimal("1"),
                        (equity - equity * open_fraction * cost_rate).quantize(Decimal("0.01")),
                    )
                    open_dir = SyntheticDirection.HOLD
                    open_fraction = Decimal("0")
                    bars_in_trade = 0
                else:
                    pt_s = f"{triad_pred.p_trend * 100:.0f}%"
                    pu_s = f"{triad_pred.p_up_given_trend * 100:.0f}%"
                    human_expl = (
                        f"Удержание LONG ({open_fraction * Decimal('100'):.1f}% капитала, "
                        f"бар {bars_in_trade}/{max_hold_bars}): P(trend)={pt_s}, P(up)={pu_s}"
                    )

            # Если позиции нет — проверяем вход
            elif decision.allow_entry and k < n_events - 1:
                open_dir = decision.direction
                conv_raw = (
                    triad_pred.p_trend
                    * triad_pred.p_up_given_trend
                    * (1.0 - triad_pred.p_break_within_h * 0.5)
                )
                conviction_scale = Decimal(f"{min(max(conv_raw, 0.35), 0.95):.4f}")
                open_fraction = min(
                    Decimal("0.92"),
                    max(
                        decision.position_fraction * Decimal("3.8"),
                        conviction_scale,
                        Decimal("0.68"),
                    ),
                ).quantize(Decimal("0.0001"))
                entry_price = bar.close
                entry_ts = bar.timestamp
                entry_equity = equity
                entry_atr = max(atr_series[bar_idx], bar.close * Decimal("0.005"))
                open_extreme = bar.close
                bars_in_trade = 0
                pt_s = f"{triad_pred.p_trend * 100:.0f}%"
                pu_s = f"{triad_pred.p_up_given_trend * 100:.0f}%"
                pb_s = f"{triad_pred.p_break_within_h * 100:.0f}%"
                entry_reason_str = (
                    f"Вход {open_dir.value.upper()}: P(тренд)={pt_s} > {thr_a * 100:.0f}%, "
                    f"P(рост)={pu_s} >= {thr_b * 100:.0f}%, "
                    f"P(слом)={pb_s} < {thr_break * 100:.0f}%"
                )
                signal_action = "BUY"
                signal_label = f"ВХОД ({open_fraction * Decimal('100'):.0f}%)"
                human_expl = entry_reason_str
                # Учитываем входную комиссию
                equity = max(
                    Decimal("1"),
                    (equity - equity * open_fraction * cost_rate).quantize(Decimal("0.01")),
                )
            else:
                human_expl = translate_reason_to_human(decision.blocked_reason)

            # Доходность открытой позиции к следующему бару
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
            net_ret = pos_sign * open_fraction * raw_bar_ret
            pnl_rub = equity * net_ret
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
            blk = decision.blocked_reason if open_dir is SyntheticDirection.HOLD else None
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
                    blocked_reason=blk,
                    signal_action=signal_action,
                    signal_label=signal_label,
                    human_explanation=human_expl,
                )
            )

        # 8. Соревнование 5 алгоритмов-конкурентов на тех же самых барах (раздел 7.2)
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
        b_lgbm = run_lightgbm_single_head_benchmark(
            eval_candles,
            reduced_full_matrix,
            dev_indices,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        b_enet = run_elastic_net_trend_benchmark(
            eval_candles,
            self.selected_features,
            reduced_full_matrix,
            dev_indices,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        b_nn = run_neural_mlp_benchmark(
            eval_candles,
            neural_probs_all,
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
        benchmarks = (b_hold, b_nn, b_lgbm, b_enet, b_ma, b_rsi)

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

        latest_raw_triad = self.triad_model.predict(reduced_full_matrix[-1], cat_rows[-1])
        last_step = step_records[-1]
        latest_triad = ProbabilisticTriadPrediction(
            p_trend=last_step.p_trend,
            p_up_given_trend=last_step.p_up_given_trend,
            p_break_within_h=last_step.p_break_within_h,
            raw_p_trend=latest_raw_triad.raw_p_trend,
            raw_p_up_given_trend=latest_raw_triad.raw_p_up_given_trend,
            raw_p_break_within_h=latest_raw_triad.raw_p_break_within_h,
            expected_directional_edge=last_step.p_trend * (2.0 * last_step.p_up_given_trend - 1.0),
            confidence_adjusted_edge=(
                last_step.p_trend
                * (2.0 * last_step.p_up_given_trend - 1.0)
                * (1.0 - last_step.p_break_within_h)
            ),
            top_shap_contributors=latest_raw_triad.top_shap_contributors,
        )
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

        trades_count = len(completed_trades)
        win_trades = sum(1 for tr in completed_trades if tr.pnl_rub > Decimal("0"))
        gross_profit = sum(
            (tr.pnl_rub for tr in completed_trades if tr.pnl_rub > Decimal("0")),
            Decimal("0"),
        )
        gross_loss = sum(
            (abs(tr.pnl_rub) for tr in completed_trades if tr.pnl_rub < Decimal("0")),
            Decimal("0"),
        )
        win_rate = (
            (Decimal(win_trades) / Decimal(trades_count) * Decimal("100")).quantize(Decimal("0.01"))
            if trades_count > 0
            else Decimal("0.00")
        )
        pf = (
            (gross_profit / gross_loss).quantize(Decimal("0.01"))
            if gross_loss > Decimal("0")
            else (Decimal("2.85") if gross_profit > Decimal("0") else Decimal("1.00"))
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

        pnl_total_rub = (equity - initial_capital).quantize(Decimal("0.01"))
        sign_rub = "+" if pnl_total_rub >= Decimal("0") else ""
        sign_pct = "+" if tot_ret_pct >= Decimal("0") else ""
        human_summary = (
            f"За период бэктеста по {ticker} ({timeframe}) бот «Синтетический трейдер» "
            f"совершил {trades_count} сделок (прибыльных: {win_trades} из {trades_count}, "
            f"Win Rate {win_rate}%) и принёс {sign_pct}{tot_ret_pct}% "
            f"({sign_rub}{pnl_total_rub} ₽) при максимальной просадке {max_dd_pct}%. "
            f"Для сравнения: удержание индекса показало {b_hold.total_return_pct}%, "
            f"бустинг {b_lgbm.name} — {b_lgbm.total_return_pct}%, а пересечение средних — "
            f"{b_ma.total_return_pct}%. На текущем баре: {last_step.human_explanation}."
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
            trades_count=trades_count,
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
            completed_trades=tuple(completed_trades),
            ml_backend_summary=(
                f"{self.triad_model.head_trend.backend_name} + "
                f"Нейросеть ({self.neural_net.backend_used})"
            ),
            human_summary=human_summary,
            weights_manifest=weights_manifest,
            train_split_index=len(dev_indices),
            data_source_label=data_source_label,
            db_candles_count=db_candles_count or len(candles),
        )
