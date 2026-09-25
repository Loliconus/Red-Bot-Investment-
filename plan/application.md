# APPLICATION — техническая спецификация слоя оркестрации

Это фиксирующий документ для слоя `application/`. Если `core/` — это "мозг" (что решать), то `application/` — это "нервная система" (когда и в каком порядке дёргать мозг, откуда брать данные и куда девать результат). Application-слой **оркестрирует**, но не содержит торговой логики — ни одного `if rsi > 70` здесь быть не должно, это всё уже посчитано в `core/`.

---

## 1. Назначение и границы

### 1.1. Что application ДЕЛАЕТ
- Реализует use cases — сценарии верхнего уровня ("прогнать цикл анализа", "разместить ордер", "заархивировать старые снапшоты").
- Владеет **композицией зависимостей** — единственное место, где конкретные адаптеры связываются с портами ядра.
- Управляет конкурентностью — расписание, `TaskGroup`, фоновые задачи, graceful shutdown.
- Транслирует между "языком ядра" (dataclasses, Decimal, Protocol) и "языком инфраструктуры" (то, что нужно адаптерам), не пропуская детали адаптеров обратно в ядро.
- Решает, **что и когда сохранять** через `RepositoryPort` — ядро только формирует снапшоты, `application/` вызывает `save_*`.

### 1.2. Что application НЕ ДЕЛАЕТ
- Не содержит формул индикаторов, не считает confluence score, не решает "входить или нет" по существу — только вызывает соответствующие функции `core/`.
- Не знает деталей протокола gRPC, схемы таблиц DuckDB, разметки HTML — эти детали инкапсулированы в `adapters/`.
- Не хранит бизнес-состояние дольше одного вызова use case (никаких module-level мутабельных переменных с котировками) — единственный источник правды о состоянии — `RepositoryPort`.

---

## 2. Композиция зависимостей (Composition Root)

### 2.1. Принцип

Вся конфигурация DI-контейнера должна быть централизована в едином, выделенном модуле (`di.py`), который выступает Composition Root — единственным местом, где собирается весь граф объектов приложения. Domain-сервисы остаются "чистыми" POPO (Plain Old Python Objects), без фреймворк-декораторов внутри бизнес-логики — вызовы контейнера ограничиваются точками входа приложения (роутеры, CLI-команды, планировщик).

Для проекта такого масштаба (один разработчик, не микросервисная система) полноценный DI-фреймворк — избыточность. Композиция собирается **вручную**, обычным Python, без библиотек типа `dependency-injector`/`inject`:

```python
# application/composition.py — единственный файл, который знает "весь мир"

@dataclass(slots=True, kw_only=True)
class AppContext:
    """Единый контейнер собранных зависимостей, передаётся в use cases явно."""
    market_data: MarketDataPort
    broker: OrderExecutionPort
    repository: RepositoryPort
    archive: ArchivePort
    clock: ClockPort
    notifier: NotificationPort | None
    settings: Settings

def build_context(settings: Settings) -> AppContext:
    """Единственное место, где адаптеры конкретно инстанциируются и связываются с портами."""
    match settings.execution_mode:
        case ExecutionMode.LIVE:
            market_data = TBankMarketDataAdapter(settings.tbank)
            broker = TBankBrokerAdapter(settings.tbank)
        case ExecutionMode.SANDBOX:
            market_data = SandboxMarketDataAdapter(settings.tbank)
            broker = SandboxBrokerAdapter(settings.tbank)
        case ExecutionMode.BACKTEST:
            market_data = BacktestReplayAdapter(settings.backtest)
            broker = SimulatedBrokerAdapter(settings.backtest)

    return AppContext(
        market_data=market_data,
        broker=broker,
        repository=DuckDBRepository(settings.storage),
        archive=ParquetArchiveAdapter(settings.storage),
        clock=SystemClock(),
        notifier=TelegramNotifier(settings.telegram) if settings.telegram else None,
        settings=settings,
    )
```

Domain-сервисы и инфраструктурные модули не должны ничего знать о `composition.py` — если сервис импортирует что-то из composition-модуля, это архитектурная инверсия и риск циклических зависимостей. Правило проекта: **зависимость может идти только в одну сторону** — `adapters/` и `application/` знают про `core/`, но не наоборот, и `core/` никогда не импортирует `application/composition`.

### 2.2. Инъекция — через конструктор use case, явно

Никакой магии с автоматическим резолвом — каждый use case получает нужные порты явно через конструктор или параметры функции. Простой конструктор облегчает тестирование — паттерн, применимый и без DI-фреймворка:

```python
# application/use_cases/run_analysis_cycle.py
class RunAnalysisCycle:
    def __init__(
        self,
        market_data: MarketDataPort,
        repository: RepositoryPort,
        clock: ClockPort,
        indicator_registry: IndicatorRegistry,
    ) -> None:
        self._market_data = market_data
        self._repository = repository
        self._clock = clock
        self._indicators = indicator_registry

    async def execute(self, instrument: Instrument) -> DecisionSnapshot:
        ...
```

Это даёт тривиальное юнит-тестирование use case: в тестах передаются фейковые реализации портов (обычные классы с нужными методами, без библиотек мокирования), а не боевые адаптеры.

---

## 3. Структура `application/`

```
application/
├── composition.py                 # Composition Root — единственная точка сборки DI
├── dto.py                         # DTO для передачи данных между use case и driving-адаптерами
├── use_cases/
│   ├── ingest_market_data.py      # получение и первичная обработка котировок
│   ├── run_analysis_cycle.py      # главный цикл: снапшот → решение → (опц.) TradePlan
│   ├── place_order.py             # исполнение одобренного TradePlan
│   ├── monitor_active_positions.py # мониторинг hard stop / invalidation / time exit
│   ├── run_backtest.py            # прогон стратегии на исторических данных
│   ├── run_hypothesis_validation.py # walk-forward проверка гипотез самоанализа
│   ├── archive_old_snapshots.py   # hot → warm → cold
│   ├── generate_daily_report.py   # агрегаты для GUI-дашборда
│   └── apply_hypothesis.py        # применение подтверждённой гипотезы (только с ручным confirm)
├── scheduler.py                   # asyncio.TaskGroup, фоновые циклы, graceful shutdown
└── events.py                      # шина доменных событий (pub/sub внутри процесса)
```

---

## 4. Ключевые use cases — точное поведение

### 4.1. `ingest_market_data.py`

Отвечает за получение котировок по всем активным инструментам и фреймам, приведение к доменному `OHLCV`, обновление hot-кэша через `RepositoryPort`. Разделяет два режима:
- **Полинг** для 1D/1H (не нужен realtime, цикл раз в N минут);
- **Стрим** для 1m и стакана (через `MarketDataPort.stream_candles`, длительно живущий цикл).

### 4.2. `run_analysis_cycle.py` — главный оркестрирующий сценарий

Псевдокод потока данных (детали формул — в `core/`, здесь только порядок вызовов):

```python
async def execute(self, instrument: Instrument) -> DecisionSnapshot:
    now = self._clock.now()

    # 1. Собираем сырые данные через порт (адаптер скрыт)
    candles = {
        tf: await self._market_data.get_candles(instrument, tf, ...)
        for tf in (Timeframe.D1, Timeframe.H1, Timeframe.M1)
    }
    orderbook = await self._market_data.get_orderbook(instrument)

    # 2. Считаем индикаторы (чистые функции из core/analysis)
    indicators = self._indicators.calculate_all(candles)

    # 3. Формируем MarketSnapshot (доменный value object)
    snapshot = build_market_snapshot(instrument, candles, indicators, orderbook, now)

    # 4. Передаём в core/strategy — получаем решение
    decision = evaluate_strategy(snapshot, config=self._strategy_config)

    # 5. Персистентность — ответственность application, не core
    snapshot_id = await self._repository.save_market_snapshot(snapshot)
    decision_snapshot = attach_snapshot_id(decision, snapshot_id)
    await self._repository.save_decision_snapshot(decision_snapshot)

    return decision_snapshot
```

Именно здесь проходит граница ответственности: `core/` вычисляет и возвращает immutable-объекты, `application/` решает, что с ними делать дальше (сохранить, передать в следующий use case, отправить в GUI через events).

### 4.3. `place_order.py`

Принимает одобренный `TradePlan` (уже прошедший риск-фильтры внутри `run_analysis_cycle`/`core/risk`), вызывает `OrderExecutionPort.place_order`, обрабатывает `OrderResult`, обновляет статус `TradePlan` через `RepositoryPort`, публикует доменное событие `OrderPlaced` в `events.py` для подписчиков (GUI WebSocket, журнал).

### 4.4. `monitor_active_positions.py` — долгоживущий цикл

Отдельный постоянный цикл (не разовый вызов), который на каждой активной позиции проверяет три независимых условия из `core/risk/`: hard stop (по цене), thesis invalidation (по условию), time exit (по TTL). Это самый критичный по надёжности use case — его падение не должно останавливать весь процесс (см. раздел про Scheduler ниже).

### 4.5. `run_backtest.py`

Принимает диапазон дат и режим (`simple` / `walk_forward`), собирает `AppContext` с `BacktestReplayAdapter`+`SimulatedBrokerAdapter`, прогоняет тот же `RunAnalysisCycle`, что и в live/sandbox — **один и тот же use case для всех трёх режимов**, различается только набор адаптеров, подставленных в `composition.py`. Это прямое следствие гексагональной архитектуры: логика цикла анализа не должна дублироваться между "боевым кодом" и "кодом для бэктеста".

### 4.6. `run_hypothesis_validation.py`

Берёт `Hypothesis` в статусе `PROPOSED`, прогоняет `run_backtest` в режиме walk-forward на условии гипотезы, записывает `walk_forward_efficiency`, переводит статус в `CONFIRMED`/`REJECTED` по порогу эффективности (не хардкод — параметр конфигурации).

### 4.7. `archive_old_snapshots.py`

Периодическая фоновая задача: проверяет занятое место (через `ArchivePort`), переносит просроченные hot-данные в warm, warm — в cold Parquet-архив с партиционированием. Порог диска и retention-период — настройки, редактируемые из GUI, не константы в коде.

### 4.8. `apply_hypothesis.py`

Единственный use case, который **изменяет боевой конфиг стратегии** на основе вывода самоанализа. Обязательный предусловие — явный `confirmed_by_user: bool = True` параметр, передаваемый только из driving-адаптера GUI после нажатия кнопки пользователем. Без этого флага use case обязан кидать исключение — это программная гарантия того, что бот никогда не меняет себя сам.

---

## 5. Scheduler — конкурентность и её подводные камни

### 5.1. Почему нельзя просто засунуть всё в один `TaskGroup`

`asyncio.TaskGroup` даёт строгую семантику структурной конкурентности: если любая дочерняя задача выбрасывает необработанное исключение, группа немедленно инициирует каскадную отмену всех остальных задач. Это отличная защита для коротких пакетов задач, но **опасна для долгоживущих фоновых циклов**: TaskGroup для потока событий, который живёт бесконечно, создаёт риски стабильности, если не спроектирован со строгими механизмами отказоустойчивости — один сбойный цикл `monitor_active_positions` уронит вместе с собой и `ingest_market_data`, если оба находятся в одной группе без изоляции ошибок.

### 5.2. Решение — изоляция на уровне каждой задачи, TaskGroup только верхнего уровня

Правильный паттерн — оборачивать каждую долгоживущую корутину в защитную обёртку, которая ловит исключения внутри себя и не даёт им всплыть в `TaskGroup`, логируя и перезапуская вместо падения всей группы:

```python
# application/scheduler.py

async def _resilient_loop(name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Изолирует сбои одного фонового цикла от остальных."""
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise  # отмену пропускаем наверх, это не ошибка
        except Exception:
            logger.exception(f"Background loop '{name}' crashed, restarting in 5s")
            await asyncio.sleep(5)

async def run(ctx: AppContext) -> None:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_resilient_loop("market_data", lambda: ingest_market_data_loop(ctx)))
        tg.create_task(_resilient_loop("analysis", lambda: analysis_cycle_loop(ctx)))
        tg.create_task(_resilient_loop("position_monitor", lambda: monitor_positions_loop(ctx)))
        tg.create_task(_resilient_loop("archiver", lambda: archiver_loop(ctx)))
```

Это соответствует общей рекомендации: перед запуском группы задач нужно чётко определить, какие дочерние задачи обязательны, а какие сбои должны становиться значением, а не исключением — в торговом боте `monitor_active_positions` жизненно обязателен, а его временный сбой должен решаться перезапуском цикла, а не падением всего процесса.

### 5.3. Обработка исключений верхнего уровня — через `except*`

Для случаев, когда падение действительно фатально (например, ошибка инициализации соединения при старте, а не в фоновом цикле), используется `except*`, появившийся вместе с `TaskGroup` в Python 3.11 — если несколько задач упадут одновременно, `TaskGroup` соберёт все исключения в `ExceptionGroup`, и `except* ConnectionError` позволяет обработать каждую ошибку соединения по отдельности, не теряя остальные.

```python
try:
    async with asyncio.TaskGroup() as tg:
        tg.create_task(startup_check_broker_connection(ctx))
        tg.create_task(startup_check_db_connection(ctx))
except* ConnectionError as eg:
    for exc in eg.exceptions:
        logger.critical(f"Startup check failed: {exc}")
    raise SystemExit(1)
```

### 5.4. Cancellation — только `try/finally`, никогда не глотать `CancelledError`

При отмене задачи `CancelledError` возникает в следующей точке `await`; её можно перехватить только для выполнения очистки ресурсов, но она обязана быть проброшена дальше — иначе вышестоящий код не узнает, что задача была отменена. Для graceful shutdown (например, при остановке бота из GUI через kill-switch) все use case-циклы обязаны использовать `try/finally` для закрытия соединений/файлов, а не `try/except` для подавления отмены.

### 5.5. Freethreading — где реально пригодится в 3.14

Тяжёлые CPU-bound расчёты (полный walk-forward бэктест на годах истории, пересчёт confluence score по всем инструментам разом) выносятся через `asyncio.to_thread`. Начиная с Python 3.14 asyncio получил полноценную поддержку free-threaded сборки и безопасен для использования в многопоточной среде, что означает: если бот запущен на `python3.14t` (без GIL), эти вычисления реально используют несколько ядер CPU параллельно, а не просто маскируют блокировку событийного цикла, как это было бы на обычной сборке с GIL.

---

## 6. Событийная шина (`events.py`) — связь с driving-адаптерами

Application-слой публикует доменные события (не путать с `core/domain/events.py` — те описывают факт, эти — доставляют его подписчикам) через простой internal pub/sub, без брокера сообщений (излишне для одного процесса):

```python
class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: type[T], handler: Callable[[T], Awaitable[None]]) -> None:
        self._subscribers[event_type].append(handler)

    async def publish(self, event: object) -> None:
        for handler in self._subscribers[type(event)]:
            await handler(event)
```

Web GUI (driving-адаптер) подписывается на события (`DecisionMade`, `OrderPlaced`, `PositionClosed`, `HypothesisConfirmed`) при старте, чтобы транслировать их в WebSocket без polling — use cases просто вызывают `event_bus.publish(...)`, не зная о существовании GUI вообще.

---

## 7. DTO — граница между application и driving-адаптерами

`application/dto.py` содержит DTO (Data Transfer Objects), которые НЕ являются доменными сущностями — это специально урезанные/сериализуемые представления для передачи в Web GUI:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlanDTO:
    """Для рендера в GUI — только то, что нужно показать, ничего лишнего."""
    id: str          # UUID как строка для JSON
    ticker: str
    entry_price: str  # Decimal как строка — не терять точность в JSON
    status: str
    unrealized_pnl_pct: str | None
```

Правило: `core/domain/entities.py` **никогда** не сериализуется напрямую в JSON для GUI — всегда через явный DTO-маппер в `application/`. Это защищает домен от нежелательной связанности с форматом API GUI (если завтра GUI сменится на другой фреймворк — домен не пострадает).

---

## 8. Соглашения об именовании

| Категория | Правило | Пример |
|---|---|---|
| Use case классы | глагол в повелительном наклонении + существительное, `PascalCase` | `RunAnalysisCycle`, `PlaceOrder` |
| Use case файлы | snake_case, совпадает по смыслу с классом | `run_analysis_cycle.py` |
| Метод запуска use case | всегда `execute()`, единая точка входа | `await use_case.execute(...)` |
| Фоновые циклы в scheduler | суффикс `_loop` | `analysis_cycle_loop`, `archiver_loop` |
| DTO-классы | суффикс `DTO` | `TradePlanDTO`, `HypothesisDTO` |
| События шины | причастие прошедшего времени, `PascalCase` | `OrderPlaced`, `PositionClosed` |
| Контекст приложения | всегда `ctx: AppContext`, единообразно во всех use case | `async def execute(self, ctx: AppContext)` |
| Конфиг-объект | всегда `settings`, никогда `config`/`cfg`/`conf` вперемешку | `settings.execution_mode` |

---

## 9. Тестирование application-слоя

`tests/unit/application/` — use cases тестируются с **фейковыми портами** (in-memory реализации `Protocol` из `core/ports/`), без реального DuckDB/gRPC. Проверяется корректность **последовательности вызовов** (например, что `save_market_snapshot` вызван раньше `save_decision_snapshot`, что `apply_hypothesis` кидает исключение без `confirmed_by_user=True`), а не сами формулы — те уже покрыты юнит-тестами `core/`.

`tests/contract/` — здесь же живут контрактные тесты, гоняющие один и тот же набор проверок против **всех** реализаций одного порта (TBank/Sandbox/Backtest для `MarketDataPort`) — это гарантия того, что `run_backtest` и live-режим используют идентично работающий контракт, а не просто "предполагается, что похоже".

---

## 10. Движение данных через слой (сводная схема)

```
[Scheduler: resilient_loop]
        │
        ▼
[Use Case.execute(ctx)] ──uses──▶ ctx.market_data (порт, реализация скрыта)
        │
        ├──▶ core/* (чистые вычисления, immutable результаты)
        │
        ├──▶ ctx.repository.save_*(...)         (персистентность)
        │
        ├──▶ event_bus.publish(DomainEvent)      (для GUI/подписчиков)
        │
        └──▶ ctx.notifier.send(...) (опционально, если критичное событие)
```

---

## Резюме: почему это решает исходные задачи

- **Единая точка сборки** (`composition.py`) — переключение live/sandbox/backtest — это выбор ветки в одном месте, а не правки по всему проекту.
- **Изолированные resilient-циклы** — падение одного фонового процесса (например, временный сбой gRPC-стрима) не останавливает мониторинг открытых позиций — критично для риск-модуля, который не должен "засыпать" никогда.
- **Один use case для live/sandbox/backtest** — гарантия, что бэктест реально проверяет ту же логику, что торгует вживую, а не отдельную параллельную реализацию, которая могла разойтись.
- **DTO-граница** — домен защищён от утечки деталей формата GUI, что облегчает будущую замену/расширение интерфейса администрирования без риска для торговой логики.