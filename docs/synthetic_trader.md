# Документация подсистемы «Синтетический трейдер»

Подсистема вероятностной оценки рыночного режима **«Синтетический трейдер»** (`core/synthetic/`) реализует переход от точечного угадывания цены закрытия к одновременной оценке **трёх калиброванных вероятностей** на каждом закрытом баре:

1. $P(\text{trend})$ — вероятность того, что рынок находится в направленном режиме (в противоположность боковому шуму / «пиле»).
2. $P(\text{up} \mid \text{trend})$ — условная вероятность движения вверх при условии наличия направленного режима.
3. $P(\text{break within } H)$ — вероятность слома или разворота текущего тренда на горизонте $H$ баров.

---

## 1. Честный инженерный аудит: что реализовано «по-взрослому», а где есть архитектурные компромиссы и фолбэки

В соответствии с правилом гексагональной архитектуры репозитория (`AGENTS.md`), пакет `core/` **не имеет права импортировать внешние библиотеки** (`numpy`, `scipy`, `torch`, `catboost`, `pandas` и др.) и обязан работать на чистой стандартной библиотеке Python 3.14 с использованием `Decimal` для всех денежных и ордерных величин.

Ниже приведён полностью прозрачный разбор того, какие узлы реализованы точными математическими алгоритмами, а где присутствуют упрощения или резервные механизмы (fallbacks):

### 1.1. Узлы с точной математической реализацией (без заглушек)
- **`core/synthetic/feature_store.py` (`PointInTimeFeatureStore`)**: полноценное индексирование свечей по времени **полного закрытия** `close_ts = open_ts + duration(tf)` с бинарным поиском `bisect_right` за $O(\log N)$ и автоматическим аудитором `verify_no_lookahead_leakage`, искажающим будущие бары и проверяющим побитовую неизменность признаков.
- **`core/synthetic/labeling.py` (`solve_l1_trend_filter`, `build_triple_barrier_events`)**:
  - Настоящий выпуклый решатель двойственной задачи $\ell_1$-фильтрации тренда (Kim, Koh, Boyd, Gorinevsky, 2009) методом проекционного покоординатного спуска $\min_{\|\nu\|_\infty \le \lambda} \frac{1}{2}\|y - (D^{(2)})^T \nu\|_2^2$ с вычислением разрыва двойственности (`dual_gap`).
  - Полная траекторная разметка методом тройного барьера (López de Prado, 2018) с Wilder ATR и фиксацией `[t_start, t_end]`.
- **`core/synthetic/feature_selection.py`**: настоящий покоординатный спуск с оператором мягкого порога (soft-thresholding) для Elastic Net ($\ell_1 + \ell_2$), проверка устойчивости на скользящих временных окнах (Walk-Forward Stability Selection) и фильтрация мультиколлинеарности по матрице корреляций Пирсона ($|r| > 0.90$).
- **`core/synthetic/hmm_regime.py` (`HamiltonMarkovRegimeDetector`)**: настоящий алгоритм Баума — Уэлша (forward-backward EM в логарифмической шкале с защитой `logsumexp`) и каузальный фильтр Гамильтона (1989) по 3 скрытым состояниям (`TREND`, `CHOP`, `PANIC`).
- **`core/synthetic/calibration.py` (`ProbabilityCalibrator`)**: настоящий подбор температуры $T$ минимизацией отрицательного лог-правдоподобия (NLL) и настоящий алгоритм **PAVA (Pool Adjacent Violators Algorithm)** для непараметрической монотонной изотонической регрессии, расчёт Brier Score, Brier Skill Score, ECE и 10-бинной Reliability Diagram.
- **`core/synthetic/validation.py`**: точная реализация Purged K-Fold + Embargo, комбинаторного разбиения $\binom{N}{k}$ CPCV, расчёта PBO методом CSCV (Bailey et al., 2017), Deflated Sharpe Ratio с поправкой на асимметрию, эксцесс и распределение Гумбеля максимума по $N_{\text{trials}}$ попыткам, а также стационарного блочного бутстрэпа Поли — Романо для тестов **White's Reality Check (2000)** и **Hansen's SPA (2005)**.

### 1.2. Компромиссы, ограничения и фолбэки (признаёмся честно)
1. **Движок CatBoost и ML-библиотеки (`core/synthetic/catboost_model.py`, `benchmarks.py`, `feature_selection.py`, `calibration.py`)**:
   - В `pyproject.toml` подключён полноценный научный и ML-стек под Python 3.14 (`numpy>=2.1`, `scipy>=1.14`, `scikit-learn>=1.6`, `catboost>=1.2.10`, `lightgbm>=4.6`).
   - При наличии установленных библиотек `SingleHeadCatBoostClassifier` обучает нативный C++ `catboost.CatBoostClassifier` (или Cython-бустер `sklearn.ensemble.HistGradientBoostingClassifier`), `feature_selection.py` использует `sklearn.linear_model.ElasticNet`, `calibration.py` использует `scipy.optimize.minimize_scalar` и `sklearn.isotonic.IsotonicRegression`, а `benchmarks.py` запускает `lightgbm.LGBMClassifier`.
   - Параллельно сохраняется встроенный движок симметричных деревьев (Oblivious Decision Trees) с точным расчётом `TreeSHAP` ($2^D$ коалиций), чтобы система продолжала работать даже в минимальном окружении без C++ колёс.
2. **Продвинутые архитектуры Этапов 3–5 (`core/synthetic/advanced_models.py`)**:
   - Классы `PatchTSTEncoder`, `TemporalFusionVariableSelector`, `CrossAssetGATBlock` и `CandlestickCNN1DExtractor` **не являются глубокими нейросетями на PyTorch/CUDA с обучением обратным распространением ошибки (backpropagation)**.
   - Поскольку `core/` не может зависеть от `torch`, эти модули реализованы как **детерминированные аналитические операторы прямого прохода**: `PatchTSTEncoder` выполняет реальное разбиение на патчи с Instance Normalization и Scaled Dot-Product Self-Attention; `TFT` вычисляет Gated Residual Network (ELU + GLU + Softmax); `CrossAssetGATBlock` считает коэффициенты внимания графа к `IMOEX` и соседям с рекуррентным гейтом `GRU`; `CandlestickCNN1DExtractor` сворачивает окно свечей фиксированными многомасштабными ядрами длины `3, 5, 7`. Их веса аналитические (фиксированные), а обучаемыми весами поверх их эмбеддингов выступают симметричные деревья CatBoost.
3. **Фолбэк эталонной серии свечей в автономном режиме (`application/use_cases/run_backtest.py`)**:
   - Если локальное хранилище DuckDB/Parquet ещё пустое (или в тестовом окружении без токена T-Invest API порт `market_data.get_candles` вернул менее 35 свечей), `run_synthetic_backtest` автоматически переключается на `generate_reference_moex_series` — детерминированный синтетический 4-фазный ряд (`TREND → CHOP → PANIC → Holdout`) для инструмента и `IMOEX`. Это сделано специально, чтобы экран `/backtest` в GUI работал сразу после клонирования репозитория без падения, но для бэктеста на реальных котировках необходимо предварительно загрузить свечи из T-Invest API.
4. **Исторический стакан в бэктесте (`adapters/driven/backtest/replay_adapter.py`)**:
   - В `BacktestReplayAdapter.get_orderbook` при отсутствии явно переданного снимка стакана выбрасывается `NotImplementedError`, так как архивы `history-data` T-Invest API содержат только минутные свечи OHLCV, но не содержат исторических L2-снимков биржевого стакана за прошлые годы.
5. **Разделение контуров принятия решений (`make_decision.py` vs `run_backtest.py`)**:
   - Боевой цикл планировщика (`run_decision_cycle` → `make_decision`) продолжает использовать детерминированный `setup_scanner` и `confluence_scorer` (чтобы сохранить 100% обратную совместимость со всеми существующими контрактными и интеграционными тестами `test_application.py`), тогда как полный конвейер «Синтетического трейдера» подключён к интерактивному бэктесту `/backtest`, API `/api/backtest/runs` и CLI `redbot backtest`.

---

## 2. Архитектура и карта модулей проделанной работы

| Модуль / Файл | Назначение |
|---|---|
| `core/synthetic/feature_store.py` | `PointInTimeFeatureStore`, `SyntheticTimeframe` (`5s`..`1mo`), `resample_candles`, `verify_no_lookahead_leakage` (раздел 1.3 ТЗ). |
| `core/synthetic/labeling.py` | Метод тройного барьера (`build_triple_barrier_events`) и выпуклая $\ell_1$-фильтрация тренда (`solve_l1_trend_filter`) (раздел 2 ТЗ). |
| `core/synthetic/features.py` | Признаки Слоя A (лог-доходности, ATR, реализованная волатильность, z-score/дельта объёма, 13 опорных SMA/EMA в относительных % и наклонах, календарь) и Слоя B (RSI 7/14/21, MACD, Stochastic, Bollinger, OBV, VWAP, уровни, кросс-актив к `IMOEX`) (раздел 3.1–3.2 ТЗ). |
| `core/synthetic/feature_selection.py` | Трёхступенчатый конвейер отбора признаков: `Elastic Net (ℓ1+ℓ2)` → `Walk-Forward Stability Selection` → `CatBoost TreeSHAP` + отсечение коллинеарных пар с $|r| > 0.90$ (раздел 3.3 ТЗ). |
| `core/synthetic/hmm_regime.py` | Этап 0: 3-состояния марковской модели переключения режимов Гамильтона (`TREND`, `CHOP`, `PANIC`), EM Баума — Уэлша и каузальный фильтр (раздел 4 ТЗ). |
| `core/synthetic/catboost_model.py` | Этапы 1–2: трёхголовая вероятностная модель `SyntheticCatBoostTriadModel` (`P(trend)`, `P(up\|trend)`, `P(break within H)`), Ordered Target Statistics, байесовский бутстрэп и точный расчёт `TreeSHAP` (раздел 4 ТЗ). |
| `core/synthetic/advanced_models.py` | Этапы 3–5: `PatchTSTEncoder`, `TemporalFusionVariableSelector` (TFT), `CrossAssetGATBlock` (GAT + GRU), `CandlestickCNN1DExtractor` (1D-CNN) (раздел 4 ТЗ). |
| `core/synthetic/calibration.py` | `ProbabilityCalibrator` (Temperature Scaling + Isotonic PAVA), `compute_brier_score`, `build_reliability_diagram`, ECE (раздел 4.1 ТЗ). |
| `core/synthetic/validation.py` | `partition_frozen_holdout`, `build_purged_kfold_splits`, `build_cpcv_splits`, `compute_pbo_cscv`, `compute_deflated_sharpe_ratio`, `compute_white_rc_and_hansen_spa` (раздел 5 ТЗ). |
| `core/synthetic/risk_engine.py` | Детерминированный риск-движок `evaluate_probabilistic_risk_gate`: пороги $a$ и $b$, сжатие по $(1 - P(\text{break}))$, volatility targeting, лимит дневной просадки + Kill Switch, запрет концентрации в одном секторе (`MOEX_SECTOR_CLUSTERS`), блокировка `PANIC`, трейлинг-стоп (раздел 6 ТЗ). |
| `core/synthetic/benchmarks.py` | Три обязательных параллельных эталона с учётом комиссии и проскальзывания: `Buy & Hold IMOEX`, `MA Crossover (8/21)`, `RSI(14) Bot` (раздел 7.2 ТЗ). |
| `core/synthetic/mlops.py` | Криптографический паспорт датасета SHA-256 (`build_dataset_manifest`), запись эксперимента (`build_experiment_record`), мониторинг дрейфа признаков `PSI` (`evaluate_drift_and_retraining_trigger`) (раздел 7.1–7.2 ТЗ). |
| `core/synthetic/engine.py` | Сквозной оркестратор `SyntheticTraderEngine` и отчёт `SyntheticTraderEvaluationReport`. |
| `adapters/driven/tbank/mappers.py` | Маппинг всех 14 интервалов свечей T-Invest (`SYNTHETIC_INTERVAL_TO_API_ENUM`), окон пагинации (`CANDLE_INTERVAL_MAX_WINDOW`) и индикативов (`indicative_to_domain`). |
| `adapters/driven/tbank/retry.py` | Парсер заголовков квотирования `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset` и `AdaptiveRateLimitGovernor` (раздел 1.2 ТЗ). |
| `adapters/driven/tbank/market_data_adapter.py` | Многолетняя пагинация `get_candles_paginated` и выгрузка макро-поводырей `get_indicatives` (`IMOEX`, `BRENT`) (раздел 1.1 ТЗ). |
| `adapters/driven/tbank/history_loader.py` | Загрузчик годовых ZIP-архивов минутных свечей `TInvestHistoryArchiveLoader` (`GET /history-data`) и распаковщик `parse_history_zip_bytes` с агрегацией в `5m`, `15m`, `1h`, `4h`, `1d`. |
| `adapters/driven/backtest/replay_adapter.py` | Локальный расчёт индикаторов `atr`, `sma`, `ema`, `rsi`, `bb`, `macd` строго по закрытым к `clock.now()` свечам в `Decimal`. |
| `application/use_cases/run_backtest.py` | Юзкейс `run_synthetic_backtest` для запуска вероятностного бэктеста из Web GUI и CLI. |
| `adapters/driving/web/routers/backtest.py` | HTTP/HTMX роутер `/backtest`, `POST /backtest/run`, `POST /api/backtest/runs` и шаблоны `pages/backtest.html`, `partials/backtest_report.html`. |
