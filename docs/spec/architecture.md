# Архитектура Red-Bot — как устроен код

Документ описывает **реализованное** состояние, а не пожелания. Если код и
документ расходятся — правится документ (или код, если расхождение найдено
тестом).

## Слои

```
                 ┌──────────────────────────────────────────┐
   driving  →    │ adapters/driving   Web GUI (FastAPI+WS)  │
   (кто просит)  │                    CLI (typer)           │
                 └──────────────┬───────────────────────────┘
                                ↓  use cases
                 ┌──────────────────────────────────────────┐
                 │ application     юзкейсы, composition,    │
                 │                 event bus, kill switch   │
                 └──────────────┬───────────────────────────┘
                                ↓  порты
   ┌───────────────────────────────────────────────────────────────┐
   │ core/     domain · analysis · risk · strategy · journal       │
   │           НИ ОДНОЙ внешней зависимости                        │
   └───────────────▲──────────────────────────▲────────────────────┘
                   │                          │
   driven  →  ┌────┴──────────────┐   ┌───────┴───────────────────┐
   (как делаем)│ adapters/driven  │   │ config/                   │
               │ tbank · sandbox  │   │ settings, секреты, логи,  │
               │ backtest         │   │ дефолты конфига           │
               │ storage (DuckDB, │   └───────────────────────────┘
               │ Parquet)         │
               └──────────────────┘
```

Правило направлений: `config → application → core`, `adapters → core`.
Обратных импортов нет — это проверяется ruff и ревью, а не «на честном слове».

## Порты (`core/ports/`) — ровно шесть

| Порт | Файл | Что делает |
| :-- | :-- | :-- |
| `MarketDataPort` | `market_data.py` | свечи, стакан, технические индикаторы API, резолв инструмента, справочник инструментов из API |
| `OrderExecutionPort` | `broker.py` | отправка/отмена заявок, статус, закрытие позиции, открытые позиции |
| `RepositoryPort` | `persistence.py` | снапшоты, планы, сделки, гипотезы, конфиг, инструменты и сохранённый каталог |
| `ArchivePort` | `archive.py` | перенос в холодный слой, компактация, бэкап, метрики слоёв |
| `ClockPort` | `clock.py` | «сейчас» — подменяется в тестах (`FrozenClock`) |
| `NotificationPort` | `notifier.py` | уведомления, включая критические |

Порт — `typing.Protocol` с `@runtime_checkable`. Адаптер реализует порт
**структурно**: достаточно иметь нужные методы, наследование не требуется.

> Деталь: `stream_candles` в протоколе объявлен как обычный `def`, возвращающий
> `AsyncIterator[OHLCV]`. Если объявить его `async def`, mypy начнёт требовать
> `Coroutine[Any, Any, AsyncIterator[...]]` и структурная проверка сломается.

## Реализации портов

| Порт | Реализации |
| :-- | :-- |
| `MarketData` | `TBankMarketDataAdapter`, `SandboxMarketDataAdapter`, `BacktestReplayAdapter`, `FakeMarketData` (тесты) |
| `OrderExecution` | `TBankBrokerAdapter`, `SandboxBrokerAdapter`, `SimulatedBroker`, `FakeBroker` (тесты) |
| `Repository` | `DuckDBRepository`, `InMemoryRepository` (тесты) |
| `Archive` | `ParquetArchive`, `FakeArchive` (тесты) |
| `Clock` | `SystemClock`, `FrozenClock` |
| `Notification` | `FakeNotifier` (тесты); Telegram — не в MVP |

## Ядро (`core/`)

| Пакет | Содержимое |
| :-- | :-- |
| `core/domain/` | `enums`, `value_objects` (Money/Price/Percentage/OHLCV/стакан), `entities` (Instrument, TradePlan, TradeThesis, Position…), `events` |
| `core/analysis/` | ATR, OBV, VWAP, Fibonacci, корреляция с бенчмарком, анализ стакана, confluence-скор, реестр индикаторов |
| `core/risk/` | модель издержек и фильтр «цель ≥ costs × multiplier», сайзинг, hard stop (структура − ATR×1.5 + трейлинг), инвалидация тезиса, time exit |
| `core/strategy/` | детектор режима (TRENDING / RANGING / HIGH_VOLATILITY), сканер сетапов, тайминг входа, сборка `TradePlan` |
| `core/journal/` | снапшоты рынка и решений, анализ сделок (MFE/MAE/exit efficiency/post-exit drift), движок гипотез с walk-forward, советы |

### Решения, которые стоит помнить

* **Деньги и цены — только `Decimal`.** `Money` и `Price` падают с `TypeError`
  при попытке передать `float`.
* **Все `datetime` — tz-aware UTC.** `MarketSnapshot` валидирует это.
* **Снапшоты — event-sourced.** История не перезаписывается: это позволяет
  потом разбирать, что бот «видел» в момент решения.
* **Hard stop проверяется первым** в `monitor_positions.decide_exit` — защита
  капитала важнее анализа («подождём, вдруг отскочит» не предусмотрено).

## Слой приложения (`application/`)

| Модуль | Роль |
| :-- | :-- |
| `composition.py` | сборка графа зависимостей (`AppContext`), выбор адаптеров по контуру |
| `events.py` | шина событий; ошибка подписчика изолируется и не роняет издателя |
| `kill_switch.py` | ручной и автоматический (дневной убыток) стоп-кран |
| `scheduler.py` | циклы с экспоненциальным backoff, без «падения насмерть» |
| `use_cases/` | один файл = один юзкейс: решение, мониторинг, исполнение, отчёт, архивация… |

## Адаптеры

### T-Invest (`adapters/driven/tbank/`)

* `tls.py` — доверие к Russian Trusted CA; `insecure_tls_dev_only` запрещён в
  `LIVE` на уровне настроек.
* `mappers.py` — protobuf ↔ домен (`Quotation` → `Decimal`, лоты ↔ штуки).
* `retry.py` — классификация ошибок; **мутация повторяется только при наличии
  ключа идемпотентности**, иначе риск удвоить сделку.
* `grpc_client.py` — ленивый импорт SDK, понятная ошибка, если пакет не стоит.
* `broker_adapter.py` — жёсткая проверка `managed_account_id`
  (`ForeignAccountError`): торговля чужим счётом невозможна по конструкции.
* `stream_manager.py` — переподписка при обрыве, соблюдение лимитов
  (300 подписок / 32 соединения).

### Хранилище (`adapters/driven/storage/`)

* `connection_pool.py` — файловый DuckDB с `memory_limit`, `threads`,
  `temp_directory`; `is_select_only` — guard read-only консоли.
* `schema.py` — DDL 11 таблиц + индексы, `SCHEMA_VERSION`.
* `duckdb_repository.py` — основная реализация `RepositoryPort`; JSON в полях
  `payload`/`reasoning` (вложенная структура меняется чаще схемы).
* `parquet_archive.py` — холодный архив, ZSTD, бэкап/восстановление.

### Бэктест и песочница

* `backtest/replay_adapter.py` — история из Parquet/DuckDB, сдвинутая на
  «текущий момент бэктеста». Стратегия не знает, что это история.
* `backtest/simulated_broker.py` — мгновенный fill с проскальзыванием и
  комиссией. Песочница не заменяет бэктест: у неё нет истории.
* `sandbox/sandbox_adapter.py` — контур песочницы T-Invest.

## GUI (`adapters/driving/web/`)

* `app.py` — сборка приложения, сессии, WebSocket, статика.
* `routers/` — `system`, `trading`, `journal`, `analysis`, `config`, `admin`.
* `websocket.py` — broadcast-хаб; мёртвые сокеты отбрасываются молча.
* Зависимости: `ContextDep` (доступ к графу), `SessionDep` (проверка сессии).
* Сессия — HMAC/TTL-токен в заголовке `X-Red-Bot-Token`; все ручки, кроме
  `/api/system/health` и логина, закрыты.
* Приложение биндится на `0.0.0.0` и не использует `TrustedHostMiddleware` —
  иначе оно не работает за внешним прокси.

## Конфигурация (`config/`)

* `settings.py` — Pydantic Settings, префикс `REDBOT_`, `extra="forbid"`,
  проверка токена/TLS; account ID может отсутствовать и выбирается по открытым счетам через API.
* Режим следующего запуска и раздельные live/sandbox account defaults хранятся в DuckDB `operational_settings`; `.env` для них не требуется.
* `secrets_source.py` — keyring как источник токена с низшим приоритетом.
* `logging_config.py` — structlog + редактирование секретов в логах.
* `seed_defaults.py` — дефолты стратегии, **загружаемые в БД** при bootstrap.
* `enums.py` — только то, что нужно composition: контуры и уровень логирования.
