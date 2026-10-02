# Синтетический трейдер · baseline 01

Статус: **исследовательская реализация, не доказанная alpha и не LIVE-система**.
Проверка окружения и официальных контрактов библиотек: **2026-10-02**.
Целевой runtime — **CPython 3.14.x**; Python 3.11–3.13 и free-threaded `3.14t`
не заявлены совместимыми. Верхняя граница `<3.15` сознательна: следующий Python
потребует отдельной проверки научных wheels и optional integrations.

## 1. Запуск без торгового токена

```bash
uv sync --locked
uv run --frozen redbot research serve --host 0.0.0.0 --port 8000
# GUI: /backtest (также /synthetic); после входа research serve открывает его.
```

Авторизация и CSRF — существующие механизмы GUI. Для публичного размещения
задайте свой секрет через `REDBOT_WEB__SESSION_SECRET`/keyring; заводской пароль
предназначен только для разработки. Research serve создаёт BACKTEST context,
не запускает decision scheduler, не подключает broker SDK и не требует токен.
Не используйте для этой цели `redbot run --mode live`.

```bash
# Быстрый полный offline smoke, с CPCV:
uv run --frozen redbot research run docs/research/configs/demo.json
# Более подробный искусственный 1H-прогон:
uv run --frozen redbot research demo --interval 1h --iterations 120
# ISS MOEX, 12 тикеров + IMOEX; не PIT-список исторических компонент!
uv run --frozen redbot research run docs/research/configs/moex.json
```

DEMO генерирует до 900 искусственных торговых дней. Его Sharpe/доходность,
даже высокие, проверяют только работоспособность инфраструктуры. Они не
подтверждают торговое преимущество. JSON configs — версионированные параметры,
а не исполняемые стратегии. Неизвестные поля, LIVE source, 1m и ложные 5/15m
отклоняются. В pilot допустима малая корзина; real example содержит 12 бумаг.

## 2. Реализовано / ограничения

| Этап ТЗ | Что действительно работает |
|---|---|
| Данные | aiomoex ISS paginated helpers, IMOEX/index + TQBR shares, read-only retry/backoff, UTC, закрытые бары, lot/tick metadata, OHLC quality checks. SHA256/Parquet immutable snapshots. |
| Иерархия | 1H + закрытые 4H/1D контексты; native 10m и 1D также поддерживаются. Higher features доступны только после конца соответствующего бара; без backward fill. |
| Метки | Next-open ATR triple barrier, TP/SL/timeout; ambiguous TP+SL исключается из direction/trend labels. Conditional direction только для trend events. Break proxy — adverse barrier против причинного EMA bias, с полным H. |
| Признаки | Return lags, TR/ATR, RV, volume, MA/EMA distances/slopes, calendar; RSI/MACD/Stoch/Bollinger/OBV/VWAP/rolling levels; IMOEX spread/correlation/beta/relative strength. Ни labels, ни retrospective ℓ1, ни timestamp/symbol попадают в модель. |
| Отбор | Window-local imputer/scaler + Elastic Net, temporal stability, native CatBoost SHAP + `abs(corr)>0.9` pruning; retrain и сравнение на nested validation. Все процедуры повторяются внутри каждого outer fold. |
| Режим | Train-only Gaussian mixture emissions + empirical Markov transitions + causal forward filter. Не HMM smoothing и не доказанная экономическая классификация trend/chop/panic. |
| Вероятности | Три CatBoost головы, balanced classes, `has_time`, early stopping; отдельный temporal isotonic calibration slice. Brier/raw Brier/train-prior Brier, reliability, conditional UP. Single-class/insufficient calibration явно отмечены. |
| CV | `purgedcv` walk-forward + full label-span purge; pre-test gap и embargo. Финальная CPCV, canonical reconstruction нескольких путей и их распределение. |
| Multiple testing | Daily net DSR: actual accumulated trials, daily Sharpe variance, skew/raw kurtosis; CSCV PBO; seeded stationary bootstrap Hansen SPA и White RC. Degenerate/no-trade tests возвращают unavailable, а не успех. |
| Исполнение | Pure stdlib/Decimal event engine: next open, lots/ticks, обе комиссии, adverse slippage, stop-first ambiguity, gaps, trailing, TTL/invalidation, общий портфель. Все open events идут прежде любого будущего close. |
| Риск | Trend/direction/break gates, panic prohibition, inverse-vol and break sizing, cost-inclusive stopped risk/net reward-risk, cash/gross/position/sector/correlation limits; daily drawdown и latched kill. |
| Frozen final | Физически отдельные последние 6–12 месяцев. Development не читает frozen Parquet. Immutable candidate/report/model/source fingerprints в SQLite, atomic reveal before read, no fit/selection/recalibration. Ошибка/отмена после reveal навсегда потребляет holdout. |
| GUI | Реальные отчёты/графики вероятностей, капитала, reliability, SHAP, gates, CPCV; config form, история, progress/cancel, защищённые artifacts и точное подтверждение final. Старый отчёт очищается при выборе нового незавершённого прогона. |
| MLOps baseline | Dataset/feature/source hashes, append-only trial ledger, saved predictions/facts/model bundle, retrospective PSI. Optional local SQLite MLflow tracking + custom calibrated-bundle registry; opt-in Prefect flow/schedule. CI safety/causality/tests, не автоматическое продвижение. |

### Не реализовано и не скрыто за заглушками

- **Доказательство доходности на реальном рынке**; независимый untouched real
  final, corporate actions/dividends/total-return audit, historical lot/sector
  metadata, PIT index composition и delistings/survivorship audit.
- **Exact 5m/15m execution feed.** ISS native intervals: 1/10/60/24/7/31/4;
  10m нельзя честно превратить в 5/15m. 1m сознательно не используется. Нужен
  отдельный проверенный источник 5/15m с достаточной историей; не синтезируем
  отсутствующие high/low, объём и внутрибара порядок.
- Neural/foundation benchmarks, PatchTST/TFT, GAT+GRU/TCN/CNN и GNN interactions;
  они не импортированы как «работающие улучшения». Сначала доказать дешевый baseline.
- Hosted Feast/DVC/W&B service, online drift/alerts/auto-retraining, постоянный
  shadow runner, broker order routing новой модели и production rollout.
  Локальные hash/PIT artifacts и PSI — baseline-аналоги, не полная инфраструктура.
- Short/margin/funding, стакан/очередь/market impact и реальная latency.

Существующие API/SDK adapters, live risk/supervisor/SQL/security guards сохранены.
Legacy Confluence trading path остаётся отдельным legacy режимом; вероятностная
модель не подключается к нему автоматически. Это намеренный запрет необоснованного
LIVE rollout, а не утверждение, что старая стратегия теперь обучена ML.

## 3. Anti-leakage контракт

- Все joins используют **availability/end**, не begin higher-bar; timezone и
  nanosecond unit канонизируются (Pandas 3/DuckDB UTC vs Etc/UTC).
- Dataset snapshot делится **до** вычисления features/labels. В development tail
  unresolved H censored; `label_end` включает полный H даже при раннем TP/SL,
  поскольку break target продолжает смотреть на весь горизонт.
- Одинаковый `asof` всех тикеров — одна CV группа. Purging учитывает наиболее
  длинный span каждой группы. Split audits проверяются библиотекой `purgedcv`.
- Fit / tuning-validation / calibration / outer-test — четыре различные роли.
  Ни imputer/scaler, ни regime emissions, Elastic Net, SHAP, pruning, early
  stopping, isotonic не fit на outer test. В stability windows unresolved labels
  также не используются.
- Очищенный набор принимается только по internal validation; выбранный набор
  обоих вариантов и честный outer Brier сохраняются в audits. Число признаков
  ограничено 8–300 (`max_features`), но не добивается искусственно до 100.
- Risk trend threshold фиксируется **до** outer OOS. Отчёт альтернативных заранее
  объявленных thresholds — multiple-testing диагностика, не выбор OOS winner.
  Автоматического выбора лучшего threshold на outer/final нет.
- CPCV использует и будущие train-группы: это зависимые комбинаторные diagnostics,
  **не** deployable chronological evidence и не замена walk-forward/final.
- PBO/SPA/White family здесь — текущие предобъявленные thresholds; это не полная
  матрица всех исторических forecaster hyperparameters. DSR trials накапливаются
  по **всем** конфигурациям источника, не сбрасываются сменой universe/interval;
  variance использует max текущей/ledger cross-trial daily SR variance и sampling
  floor. Это консервативная оценка, не магическое устранение зависимости trials.
  `additional_trials` — cumulative число внешних/off-ledger попыток; максимум
  деклараций сохраняется. Скрытые ручные попытки математически не обнаружить.
- Price-jump diagnostics development не сообщают даже факт такого скачка в
  frozen части. Техническая нормализация на ingestion не обучает модель.

## 4. Риск, benchmark и метрики

Головы оценивают barrier/reversal **proxies**, не «истинную вероятность тренда»
и не accuracy как финансовую цель. Sizing confidence =
`trend × max(0, 2×up−1) × (1−break)` с inverse-vol и независимыми caps.
Стоп-риск включает adverse stop execution и обе комиссии; minimum net
reward/risk учитывает costs. Сигнал закрытого бара входит только на следующем
доступном open. Stop не повышается high этого же незавершённого бара; гэп
исполняется по худшему open, не по недоступному trigger. Overnight gap входит
в дневной DD. Kill остаётся latched до конца отдельного backtest run.

Дневной net return, Sharpe `sqrt(252)` с risk-free=0, daily drawdown, CAGR
(только достаточно длинный период), fees, trades, profit factor и причины
отказа — отдельно. Daily DD отчёта не заменяет внутридневной risk limit.
Издержки money ledger — Decimal; float начинается на ML/statistics/plot boundary.
ATR для stop/target/sizing пересчитывается по raw Decimal OHLC (Wilder RMA),
а не берётся из float feature ATR. ML/statistical ATR не задаёт денежную цену.

MA cross и RSI используют тот же portfolio ledger/risk/costs/capital/calendar.
IMOEX — **теоретический дробный price-index reference**, не доступный торгуемый
индекс/ETF и не total-return benchmark: buy/hold с entry/exit costs, Decimal
расчёт стоимости до statistical conversion. Dividends не включены ни в reference,
ни в необработанные shares. Sector map — coarse current proxy; неизвестные бумаги
попадают в одну conservative группу. Current lot/tick metadata нельзя выдавать
за PIT исторические значения. Любая alpha gate всё равно требует data audit.

## 5. Однократный final

```bash
# Только после фиксации конфигурации и модели! Цена ошибки — потерянный holdout.
uv run --frozen redbot research final <32-hex-run-id> \
  --confirmation "ОТКРЫТЬ FINAL OOS"
```

CLI и GUI используют тот же immutable candidate и atomic SQLite gate. Проверяются
реальные bytes `bundle.json`/трёх CBM, полный report/config, исходники и версии
научных библиотек. Изменённая модель/код/manifest не открывают final. При reveal
период источника закрывается глобально: смена start/universe/timeframe/freeze size
не делает перекрывающийся календарь untouched. После reveal **не исправляйте**
этот holdout ретюнингом. Для нового доказательства нужен новый независимый период.
Сброс базы, копия корня или удаление ledger не восстанавливают научную независимость.

Даже успешно раскрытый real final не включает LIVE: corporate/execution audit,
shadow, независимый review и отдельное внедрение order-routing обязательны.

## 6. Artifacts и эксплуатация

`data/research/` игнорируется Git; не коммитить большую историю/модели/секреты.

```text
experiments.sqlite3      trials / external declarations / study scopes / immutable candidates / reveals
worker.lock              единый CLI / GUI / Prefect mutex
 datasets/<sha256>/       manifest.json, development/*.parquet, frozen/*.parquet
 runs/<uuid>/             config/status/report.{json,md}, features/predictions.parquet
                          model/bundle.json + y_trend/y_up/y_break.cbm
                          final_status/final.json/final_predictions.parquet (лишь после reveal)
mlflow.sqlite3           optional private local tracking/registry
mlflow_artifacts/        optional artifacts, также под data/research
```

`features.parquet` — offline research rows с фактами/labels, **не online feature
store для исполнения**. `predictions.parquet` — прогноз/факт для OOS audit.
API не раздаёт config, raw/frozen data, SQLite, произвольный путь или исполняемый
pickle. GUI worker environment — whitelist без торговых/GUI/GitHub credentials,
ограничение native threads=2, subprocess lifecycle/cancel/recovery. CLI/Prefect
также используют общий mutex; crash не восстанавливает consumed final.

```bash
# Optional; по умолчанию выключено.
uv sync --locked --extra mlops
# В config включить mlflow_tracking: true; registry остаётся research/live=false.
uv run --frozen --extra mlops redbot research schedule \
  docs/research/configs/moex.json --cron "0 2 * * 1"
```

Prefect schedule включается только этой явной командой (долгоживущий runner).
Не запускается вместе с GUI самовольно; final не раскрывает, сделки не отправляет.
MLflow использует local SQLite, не deprecated filesystem metadata backend,
и регистрирует **весь calibrated bundle**, не голый CatBoost. Не публикуйте
неаутентифицированный MLflow server. Расписание требует операционного review
после first real-data run; при уже revealed study job законно откажется работать.

## 7. Проверки и воспроизводимость

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest -m "not sandbox and not e2e"
uv run --frozen playwright install chromium
uv run --frozen pytest -m e2e
```

CI ориентирован на 3.14: locked dependencies, strict typing, causality/Decimal/
purge/frozen/concurrency/worker/API tests и отдельные browser checks. Эти CI gates
предотвращают регрессии, **не** сертифицируют рыночную доходность и не promote LIVE.
Sandbox/реальные заявки в tests не отправляются.

В Arena scientific runtime действительно проверен на CPython **3.14.8**.
`uv.lock` актуализирован; запись официального SDK **1.51.0**, его source URL и
SHA256 сохранены. Обычный full sync/download SDK и ISS запросы здесь получают
TLS handshake EOF. Поэтому offline scientific/demo/tests доступны, но реальные
MOEX performance и SDK-dependent контрактные тесты в этой среде не подтверждены.
TLS verification не отключалась и SDK не подменялся устаревшим public PyPI пакетом.
`uv sync --locked --no-install-package t-tech-investments` — исключительно способ
проверить offline research часть при недоступном registry, не полноценная установка
для broker/SDK tests. Нативные environment repairs sandbox не являются частью проекта.

## 8. Официальные источники

- CPython 3.14: https://docs.python.org/3.14/whatsnew/3.14.html
- aiomoex ISS/candles/pagination: https://wlm1ke.github.io/aiomoex/build/html/api.html
- MOEX ISS limits: https://fs.moex.com/f/11744/netflow2-get-data-en.html
- Pandas 3: https://pandas.pydata.org/docs/whatsnew/v3.0.0.html
- CatBoost releases/parameters: https://github.com/catboost/catboost/releases,
  https://catboost.ai/docs/en/references/training-parameters/common
- sklearn Elastic Net: https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html
- purgedcv source/contracts/path reconstruction/DSR/CSCV: https://github.com/eslazarev/purged-cross-validation
- DSR units, raw kurtosis, expected max: https://marti.ai/qfin/2018/05/30/deflated-sharpe-ratio.html
- arch SPA/RealityCheck: https://arch.readthedocs.io/en/latest/multiple-comparison/multiple-comparison_examples.html
- OSQP convex auxiliary: https://osqp.org/docs/
- MLflow tracking/backend/registry: https://mlflow.org/docs/latest/ml/tracking/
- Prefect 3 deployment: https://docs.prefect.io/v3/how-to-guides/deployments/create-deployments
- SDK источник: https://opensource.tbank.ru/invest/invest-python
- GitHub Actions usage versions: https://github.com/actions/checkout,
  https://github.com/actions/setup-python, https://github.com/astral-sh/setup-uv

Observed scientific versions: NumPy 2.5.3, Pandas 3.0.6, SciPy 1.18.1,
sklearn 1.9.1, CatBoost 1.2.10, aiomoex 2.2.0, purgedcv 0.1.10, arch 8.0.0,
OSQP 1.1.3; optional MLflow 3.16.1 / Prefect 3.8.7. Exact full dependency
selection/hashes находятся в `uv.lock`, не в плавающей таблице README.
