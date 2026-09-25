# ADAPTERS — техническая спецификация слоя интеграций

Это фиксирующий документ для слоя `adapters/`. Здесь и только здесь разрешено знать про `grpc`, `duckdb`, `fastapi`, конкретные форматы ответов API, файловые пути и HTML. Каждый адаптер — это перевод между "грязным" внешним миром и "чистыми" контрактами портов из `core/ports/`.

---

## 1. Назначение и границы

### 1.1. Что adapters ДЕЛАЮТ
- Реализуют `Protocol`-порты из `core/ports/` конкретными технологиями (gRPC, DuckDB, FastAPI, Parquet).
- Конвертируют внешние форматы данных (units+nano, protobuf-сообщения, JSON) в доменные value objects (`Decimal`, `OHLCV`, `Money`) и обратно.
- Обрабатывают всё, что специфично для инфраструктуры: реконнекты, ретраи, TLS, SQL-запросы, HTTP-роутинг, сериализацию в JSON для GUI.
- Не содержат торговой логики — если в адаптере появляется `if rsi > 70`, это архитектурная ошибка, код принадлежит `core/`.

### 1.2. Разделение driven/driving

Driver-порты (driving) определяют, как внешние компоненты взаимодействуют с приложением, тогда как driven-порты определяют, как приложение взаимодействует с внешними системами. Применительно к проекту:

- **Driving** (управляют ботом извне): Web GUI, CLI.
- **Driven** (ими управляет бот): T-Bank gRPC API, Sandbox, DuckDB, файловая система (Parquet-архив).

```
adapters/
├── driven/       # "нас дёргают наружу" — implements Protocol из core/ports
│   ├── tbank/
│   ├── sandbox/
│   ├── backtest/
│   └── storage/
└── driving/      # "мы дёргаем наружу" — вызывают application/use_cases
    ├── web/
    └── cli/
```

---

## 2. `adapters/driven/tbank/` — интеграция с боевым API

### 2.1. Структура

```
tbank/
├── grpc_client.py         # обёртка над t-tech-investments, управление соединением
├── tls.py                 # Russian Trusted CA, ssl_channel_credentials
├── retry.py               # ретраи на UNAVAILABLE, экспоненциальный backoff
├── mappers.py              # protobuf → domain dataclasses (единственное место конвертации)
├── market_data_adapter.py # implements MarketDataPort
├── broker_adapter.py      # implements OrderExecutionPort
└── stream_manager.py       # управление bidirectional-стримом подписок
```

### 2.2. TLS — уточнённая финальная реализация

T-Invest API обслуживает TLS-сертификаты, которые цепляются к Российскому Доверенному Корневому CA (НУЦ Минцифры РФ), отсутствующему в стандартных хранилищах доверия ОС — без него соединения падают с ошибкой проверки сертификата.

```python
# tls.py
def build_grpc_credentials(ca_bundle_path: Path) -> grpc.ChannelCredentials:
    root_certs = ca_bundle_path.read_bytes()
    return grpc.ssl_channel_credentials(root_certificates=root_certs)

def build_channel(target: str, token: str, ca_bundle_path: Path) -> grpc.aio.Channel:
    call_credentials = grpc.access_token_call_credentials(token)
    channel_credentials = grpc.composite_channel_credentials(
        build_grpc_credentials(ca_bundle_path), call_credentials,
    )
    return grpc.aio.secure_channel(target, channel_credentials)
```

Переключение между контурами реализовано через параметр `target` — константа для боевого контура и отдельная константа для песочницы, что и определяет выбор адаптера в `application/composition.py` без изменения кода клиента.

### 2.3. Конвертация units+nano — только в `mappers.py`

Значения SDK, представленные как units и nano, конвертируются в `Decimal` напрямую, без промежуточного прохода через `float`. Это единственное место во всём проекте, где встречается формат `MoneyValue`/`Quotation`:

```python
# mappers.py
def quotation_to_decimal(units: int, nano: int) -> Decimal:
    return Decimal(units) + Decimal(nano) / Decimal(10**9)

def candle_to_domain(pb_candle) -> OHLCV:
    return OHLCV(
        open=quotation_to_decimal(pb_candle.open.units, pb_candle.open.nano),
        high=quotation_to_decimal(pb_candle.high.units, pb_candle.high.nano),
        low=quotation_to_decimal(pb_candle.low.units, pb_candle.low.nano),
        close=quotation_to_decimal(pb_candle.close.units, pb_candle.close.nano),
        volume=pb_candle.volume,
        timestamp=pb_candle.time.ToDatetime(tzinfo=UTC),
        timeframe=_map_interval(pb_candle.interval),
    )
```

Raw SDK response объекты не должны становиться моделью приложения — вместо этого данные из портфеля и любых других ответов сопоставляются в собственные модели приложения. Ни один protobuf-объект не пересекает границу `adapters/` → `core/`/`application/`.

**Критический нюанс**: все цены в сервисе котировок предоставляются за одну ценную бумагу, а не за лот — при расчёте стоимости позиции обязательно умножение на `lot_size` из `Instrument`. Если это перепутать в `mappers.py`, вся риск-модель в `core/` будет считать неверные суммы, даже если формулы там правильные.

### 2.4. Стрим котировок — server-side, не bidirectional

Для MVP один инструмент = одна подписка на один тип данных, без необходимости динамически переключать список подписок в реальном времени — в этом случае предпочтителен **MarketDataServerSideStream**, а не двунаправленный `MarketDataStream`, так как он проще в реализации (один запрос → поток ответов) при том же наборе данных. Bidirectional-стрим целесообразен только если понадобится на лету менять состав подписок без переоткрытия соединения — отложено до реальной необходимости (принцип "не фанатизм").

Важные технические детали, обязательные к реализации в `stream_manager.py`:
- Свечи в рамках стрима собираются "на лету" из ленты обезличенных сделок и отправляются не чаще одного раза в 100мс плюс свеча закрытия периода сбора.
- Возможны ситуации, когда сделки с биржи доходят уже после закрытия периода сбора свечи — в этом случае в стрим приходит одна или несколько «корректирующих» свечей. Адаптер обязан **обновлять**, а не дублировать последнюю сохранённую свечу при получении корректирующей — это логика `market_data_adapter.py`, не `core/`.
- Максимальное количество подписок на одно соединение ограничено лимитной политикой API — при 2-5 инструментах × 3 фрейма это не должно быть проблемой, но лимит обязательно фиксируется как константа в `Settings` для явного контроля.

### 2.5. Реконнект и ретраи — обязательный слой устойчивости

Транзиентные сбои T-Invest API со статусом gRPC `UNAVAILABLE` должны ретраиться автоматически, тогда как ошибки аутентификации и прочие не-транзиентные ошибки автоматическому ретраю не подлежат. Для стримов при обрыве соединения необходимо не просто повторить запрос, а полностью переподписаться на весь набор подписок заново — по умолчанию в референсных реализациях включён ретраер, который при ошибках от gRPC пытается повторить запрос, а для стримов переподключается и заново переподписывает весь список подписок.

```python
# retry.py
RETRYABLE_STATUS_CODES = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}

async def with_retry(fn: Callable[[], Awaitable[T]], max_attempts: int = 5) -> T:
    for attempt in range(max_attempts):
        try:
            return await fn()
        except grpc.aio.AioRpcError as e:
            if e.code() not in RETRYABLE_STATUS_CODES or attempt == max_attempts - 1:
                raise
            await asyncio.sleep(min(2 ** attempt, 30))
    raise RuntimeError("unreachable")
```

### 2.6. Изоляция счёта — единственная защита от торговли "не тем" счётом

`broker_adapter.py` обязан на входе каждого метода `place_order`/`cancel_order` проверять `account_id` против `settings.managed_account_id` и кидать исключение при несовпадении — это единственное место в проекте, физически способное отправить ордер, поэтому проверка дублируется здесь явно, несмотря на то, что `application/` тоже должен передавать верный ID.

---

## 3. `adapters/driven/sandbox/` — тестовый контур

Sandbox переиспользует те же `mappers.py` и `tls.py` из `tbank/` (это одна и та же gRPC-инфраструктура, отличается только `target`-константа подключения), но имеет собственный `sandbox_adapter.py`, реализующий тот же `OrderExecutionPort` и `MarketDataPort`. В T-Invest API боевой и тестовый контуры переключаются именно через параметр `target` соединения, а не через отдельные учётные данные или домены.

**Обязательная пометка в реализации**: класс `SandboxBrokerAdapter` должен содержать явный комментарий/константу о том, что исполнение здесь не отражает полностью реальное проскальзывание — это существенно при интерпретации результатов ранней проверки бота, не только для интеграционных тестов.

---

## 4. `adapters/driven/backtest/` — историческая симуляция

### 4.1. `replay_market_data_adapter.py`

Реализует `MarketDataPort`, но вместо сети читает исторические свечи из `cold/`-архива (DuckDB поверх Parquet). Метод `stream_candles` эмулируется генератором, который отдаёт свечи с виртуальным "текущим временем", синхронизированным через `FrozenClock`/`SimulatedClock` — это гарантирует, что `core/strategy` не видит разницы между live-стримом и воспроизведением истории.

### 4.2. `simulated_broker_adapter.py`

Реализует `OrderExecutionPort` с явной моделью исполнения:
- комиссия 0.3% за сделку — константа, идентичная боевой, берётся из `settings.cost_model`, не хардкодится отдельно от продакшен-конфига;
- проскальзывание — простая модель (например, фиксированный процент от спреда на момент исполнения, читаемый из того же исторического снапшота стакана, если он был сохранён);
- заполнение лимитных ордеров — только если цена в исторических данных фактически "прошла" уровень заявки, а не мгновенно по заявленной цене (это ключевое отличие от упрощённой логики sandbox).

---

## 5. `adapters/driven/storage/` — DuckDB и Parquet

### 5.1. Файлы

```
storage/
├── duckdb_repository.py     # implements RepositoryPort — hot/warm слои
├── parquet_archive.py       # implements ArchivePort — cold слой, партиционирование
├── schema.py                 # DDL таблиц, миграции
└── connection_pool.py        # управление подключениями, memory_limit
```

### 5.2. Инициализация — жёсткие лимиты RAM (зафиксировано пользователем)

DuckDB по умолчанию может задействовать до значительной доли физической оперативной памяти системы для операций и кеша — это обязательно ограничивается явно, поскольку ПК используется параллельно днём:

```python
# connection_pool.py
def create_connection(db_path: Path, settings: StorageSettings) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(db_path))          # файл на диске, не ":memory:"
    conn.execute(f"SET memory_limit = '{settings.duckdb_memory_limit_mb}MB'")
    conn.execute(f"SET temp_directory = '{settings.tmp_spill_dir}'")
    conn.execute(f"SET threads = {settings.duckdb_threads}")
    return conn
```

`settings.duckdb_memory_limit_mb` и `duckdb_threads` — параметры, редактируемые из GUI-раздела администрирования БД (зафиксировано в предыдущем документе), а не константы в коде.

### 5.3. Схема таблиц (`schema.py`) — точное соответствие доменным dataclasses

```sql
CREATE TABLE IF NOT EXISTS market_snapshots (
    id UUID PRIMARY KEY,
    instrument_uid VARCHAR NOT NULL,
    captured_at TIMESTAMP NOT NULL,
    ohlcv_json JSON NOT NULL,          -- dict[Timeframe, OHLCV] сериализован
    indicators_json JSON NOT NULL,     -- dict[Timeframe, dict[str, Decimal]]
    orderbook_json JSON,
    imoex_correlation DECIMAL(10, 6),
    market_regime_json JSON NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id UUID PRIMARY KEY,
    market_snapshot_id UUID NOT NULL REFERENCES market_snapshots(id),
    trade_plan_id UUID,
    decision VARCHAR NOT NULL,          -- DecisionType как строка (StrEnum!)
    reasoning_chain_json JSON NOT NULL,
    confluence_score DECIMAL(6, 4) NOT NULL,
    risk_check_passed BOOLEAN NOT NULL,
    risk_check_reason VARCHAR,
    thought_text TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_reviews (
    trade_plan_id UUID PRIMARY KEY,
    entry_price DECIMAL(18, 6) NOT NULL,
    exit_price DECIMAL(18, 6) NOT NULL,
    mfe DECIMAL(18, 6) NOT NULL,
    mae DECIMAL(18, 6) NOT NULL,
    exit_efficiency DECIMAL(6, 4) NOT NULL,
    post_exit_drift_pct DECIMAL(8, 4) NOT NULL,
    verdict VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id UUID PRIMARY KEY,
    text TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    confidence DECIMAL(6, 4) NOT NULL,
    status VARCHAR NOT NULL,
    walk_forward_efficiency DECIMAL(6, 4)
);
```

Хранение сложных вложенных структур (`ohlcv_json`, `reasoning_chain_json`) как нативного типа `JSON` в DuckDB — обоснованное решение для MVP: DuckDB поддерживает JSON-колонки с возможностью прямых SQL-запросов внутрь структуры (`json_extract`), что покрывает потребность "искать паттерны в бигдате" без необходимости заранее нормализовывать каждое вложенное поле в отдельную таблицу.

### 5.4. `parquet_archive.py` — партиционирование для cold-слоя

```python
def archive_to_parquet(conn: duckdb.DuckDBPyConnection, cutoff: datetime) -> None:
    conn.execute(f"""
        COPY (
            SELECT * FROM market_snapshots
            WHERE captured_at < '{cutoff.isoformat()}'
        ) TO 'data/cold/snapshots'
        (FORMAT PARQUET, PARTITION_BY (instrument_uid, year, month), COMPRESSION ZSTD)
    """)
    conn.execute(f"DELETE FROM market_snapshots WHERE captured_at < '{cutoff.isoformat()}'")
```

Запросы дата-майнинга поверх архива читают Parquet напрямую через `read_parquet()` с glob-паттерном по партициям — DuckDB не требует загрузки в память, отфильтрованные партиции читаются с диска по запросу.

---

## 6. `adapters/driving/web/` — FastAPI GUI

### 6.1. Структура

```
web/
├── app.py                  # инициализация FastAPI, подключение роутеров, lifespan
├── dependencies.py         # Depends() для получения AppContext в роутерах
├── routers/
│   ├── dashboard.py
│   ├── instruments.py
│   ├── risk_settings.py
│   ├── charts.py
│   ├── journal.py
│   ├── backtest.py
│   ├── db_admin.py          # раздел администрирования БД (зафиксирован ранее)
│   └── security.py
├── websocket.py             # трансляция событий EventBus в браузер
└── schemas.py                # Pydantic-модели запросов/ответов (только здесь Pydantic!)
```

### 6.2. Правило границы: Pydantic живёт только в `driving/web/`

`core/` и `application/` используют `dataclasses`; `pydantic.BaseModel` применяется **исключительно** в `schemas.py` для валидации HTTP-запросов и сериализации ответов. Роутер конвертирует `TradePlanDTO` (из `application/dto.py`) в `schemas.TradePlanResponse` явным мэппингом — никогда не передаёт доменный объект напрямую в `JSONResponse`.

### 6.3. Lifespan — единая точка запуска фоновых циклов вместе с веб-сервером

```python
# app.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = build_context(load_settings())
    app.state.ctx = ctx
    scheduler_task = asyncio.create_task(scheduler.run(ctx))
    yield
    scheduler_task.cancel()
    with suppress(asyncio.CancelledError):
        await scheduler_task
```

Это гарантирует, что `application/scheduler.py` (фоновые resilient-циклы из предыдущего документа) стартует и останавливается вместе с жизненным циклом веб-приложения, без отдельного процесса-демона — оправдано для однопользовательского локального проекта.

### 6.4. `websocket.py` — реалтайм без polling

```python
@router.websocket("/ws/events")
async def events_ws(websocket: WebSocket, ctx: AppContext = Depends(get_context)):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()

    async def handler(event: object) -> None:
        await queue.put(event)

    for event_type in (DecisionMade, OrderPlaced, PositionClosed):
        ctx.event_bus.subscribe(event_type, handler)

    try:
        while True:
            event = await queue.get()
            await websocket.send_json(serialize_event(event))
    except WebSocketDisconnect:
        pass
```

### 6.5. `db_admin.py` — администрирование хранилища (зафиксировано ранее)

Роутер вызывает исключительно `application/use_cases/` (`archive_old_snapshots`, отдельный `execute_readonly_query`), никогда не обращается к `DuckDBRepository` напрямую из веб-слоя — это сохраняет инверсию зависимостей даже для служебных функций.

```python
@router.post("/db-admin/query", response_model=QueryResultResponse)
async def run_readonly_query(req: QueryRequest, ctx: AppContext = Depends(get_context)):
    if not is_select_only(req.sql):
        raise HTTPException(400, "Only SELECT queries are allowed")
    result = await ctx.repository.execute_readonly(req.sql)
    return QueryResultResponse.from_domain(result)
```

Проверка `is_select_only` — обязательный guard, защищающий SQL-консоль GUI от случайного `DROP TABLE` через веб-интерфейс.

---

## 7. `adapters/driving/cli/` — служебный интерфейс

Минимальный CLI (`click`/`typer`) для операций, которые неудобно дёргать через браузер: разовый запуск backtest из терминала, ручная миграция БД, экспорт бэкапа. Использует тот же `composition.build_context`, что и веб-приложение — никакой отдельной логики сборки зависимостей.

---

## 8. Соглашения об именовании

| Категория | Правило | Пример |
|---|---|---|
| Адаптер-классы | Технология + суффикс имени порта | `TBankMarketDataAdapter`, `DuckDBRepository` |
| Файлы мапперов | всегда `mappers.py` внутри пакета адаптера | `tbank/mappers.py` |
| Функции конвертации | `<источник>_to_<цель>` | `quotation_to_decimal`, `candle_to_domain` |
| FastAPI роутеры | существительное во множественном числе, snake_case | `instruments.py`, `risk_settings.py` |
| Pydantic-схемы | суффикс `Request`/`Response` | `TradePlanResponse`, `QueryRequest` |
| DuckDB таблицы | snake_case, множественное число | `market_snapshots`, `trade_reviews` |
| Parquet-партиции | `key=value` формат Hive-style | `instrument_uid=SBER/year=2026/month=09` |
| Settings-поля для storage | префикс технологии | `duckdb_memory_limit_mb`, `duckdb_threads` |

---

## 9. Тестирование adapters

`tests/e2e/` — реальные вызовы к sandbox-контуру T-Invest (с настоящим TLS-бандлом, настоящим gRPC), запускаются отдельно от юнит-тестов, требуют валидного токена в CI/окружении разработчика.

`tests/contract/` — набор идентичных тестов, гоняемых против **всех** реализаций одного порта (`TBankMarketDataAdapter`, `SandboxMarketDataAdapter`, `BacktestReplayAdapter`) — проверяется, что все три возвращают `OHLCV` с одинаковой структурой и семантикой полей, что гарантирует взаимозаменяемость без сюрпризов при переключении режима.

`tests/unit/adapters/` — тестируются только чистые функции без сети/диска: `mappers.py` (units+nano → Decimal), `is_select_only` guard, сериализация DTO в JSON-схемы.

---

## Резюме: почему это решает исходные задачи

- **SSL-проблема решена системно** — весь TLS-код инкапсулирован в 30-40 строк `tls.py`, использующих официальный Russian Trusted CA бандл, а не глобальное отключение проверки.
- **Устойчивость к сбоям API** — реализованы ретраи на транзиентные `UNAVAILABLE`-ошибки и полная переподписка стрима при разрыве, что критично для многочасового/многодневного удержания позиций без пропуска важных данных.
- **RAM под контролем** — DuckDB работает как файловая, а не in-memory БД, с жёстким лимитом памяти, настраиваемым через GUI, что не мешает дневной работе за тем же ПК.
- **Администрирование БД встроено архитектурно** — SQL-консоль и управление архивацией идут через `application/use_cases`, а не напрямую в обход инверсии зависимостей.
- **Один и тот же контракт для live/sandbox/backtest** — гарантия контрактными тестами, что переключение режимов не меняет поведение `core/`/`application/`, только источник данных.