# CORE — техническая спецификация ядра проекта

Это фиксирующий документ для слоя `core/`. Ядро — центр гексагона: единственное место, где живёт торговая логика, доменные сущности и правила риска. Ядро **не имеет ни одной внешней зависимости** — ни gRPC, ни FastAPI, ни DuckDB, ни даже `t-tech-investments`. Если удалить весь остальной проект и оставить только `core/`, тесты внутри него обязаны продолжать проходить.

---

## 1. Назначение и границы

### 1.1. Что core ДЕЛАЕТ
- Хранит доменные сущности и value objects (Instrument, TradePlan, Position, Money и т.д.).
- Содержит всю торговую логику: детекцию режима рынка, поиск сетапов, тайминг входа, risk-менеджмент.
- Содержит собственные (не из API) технические индикаторы и confluence-скоринг.
- Формирует "мысли" бота (reasoning chain) и хранит доменную модель гипотез самоанализа.
- Объявляет порты (`Protocol`) — контракты, которые обязаны реализовать внешние адаптеры.

### 1.2. Что core НЕ ДЕЛАЕТ (жёсткое правило)
- Не импортирует `grpc`, `fastapi`, `duckdb`, `httpx`, `pydantic` (для доменных моделей — только `dataclasses`).
- Не знает о существовании sandbox/live режимов как о разных системах — для core это просто разные реализации одного и того же порта.
- Не пишет и не читает файлы, не делает сетевых вызовов, не имеет побочных эффектов, кроме как через явно переданные порты.
- Не содержит ни одной строки, зависящей от времени выполнения (`datetime.now()` напрямую) — только через `ClockPort`, иначе тестирование стратегий станет недетерминированным.

---

## 2. Технологические соглашения ядра

### 2.1. Value Objects — только `frozen=True, slots=True`

Для всего, что представляет собой значение (деньги, координаты цены, конфигурационные снэпшоты), используется `frozen=True` — это предотвращает изменение после создания и делает объект пригодным для использования как ключ словаря или элемент множества. Дополнительно на Python 3.10+ рекомендуется всегда включать `slots=True`, поскольку это сокращает потребление памяти на инстанс примерно на 40% и ускоряет доступ к атрибутам, а прирост производительности достаётся бесплатно.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Money:
    amount: Decimal
    currency: str = "RUB"
```

`kw_only=True` обязателен для всех доменных классов с более чем 3-4 полями — это предотвращает ошибки перепутанных позиционных аргументов, характерные при большом количестве полей.

### 2.2. Entities — `slots=True`, без `frozen`

Сущности с идентичностью и изменяемым состоянием (`Position`, `TradePlan`) используют `slots=True`, но не `frozen`, так как их состояние меняется в течение жизненного цикла (например, `TradePlan.status`). Валидация и вычисляемые поля — только в `__post_init__`, без I/O и тяжёлых вычислений внутри него.

### 2.3. Enum — `StrEnum` для доменных статусов

Для доменных статусов и типов (никогда — «магические строки») используется `enum.StrEnum`, добавленный в Python 3.11 — специализированная версия enum, где все члены являются строками, что обеспечивает совместимость со строковыми операциями при сохранении всех преимуществ enum. Это критично для сериализации в БД/JSON (в логах и снапшотах статус пишется как обычная строка без доп. маппинга) и для читаемости в GUI и SQL-запросах дата-майнинга.

```python
class TradePlanStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    CLOSED = "closed"
    REJECTED = "rejected"
```

Имена членов enum — `UPPER_CASE`, значения — `snake_case`-строки: имена в верхнем регистре — устоявшаяся конвенция для представления констант в Python.

### 2.4. Деньги и цены — только `Decimal`, никогда `float`

Все денежные величины, цены, проценты — `Decimal`. `float` в торговой логике запрещён категорически (ошибки округления накопительно искажают P&L и расчёт издержек). Единственное исключение — координаты на графике для рендеринга в GUI (эта конвертация происходит в адаптере, не в ядре).

### 2.5. Протоколы (порты) — `typing.Protocol`, не `abc.ABC`

Порты объявляются как структурные типы. Адаптер не обязан наследоваться — достаточно реализовать нужные методы с совпадающими сигнатурами. Это ключевая практика для лёгкой интеграции новых адаптеров без изменения ядра.

---

## 3. Полная структура `core/`

```
core/
├── domain/
│   ├── entities.py          # Instrument, Position, TradePlan
│   ├── value_objects.py     # Money, Price, Percentage, TimeRange, Ticker
│   ├── enums.py             # ВСЕ StrEnum проекта — единая точка правды
│   └── events.py            # доменные события: OrderFilled, ThesisInvalidated...
│
├── strategy/
│   ├── regime_detector.py
│   ├── setup_scanner.py
│   ├── entry_timing.py
│   └── trade_plan_builder.py
│
├── risk/
│   ├── cost_model.py
│   ├── position_sizing.py
│   ├── hard_stop.py
│   ├── thesis_invalidation.py
│   └── time_exit.py
│
├── analysis/
│   ├── protocols.py          # Protocol Indicator — контракт для ЛЮБОГО индикатора
│   ├── atr.py
│   ├── volume_indicators.py  # OBV, VWAP
│   ├── fibonacci.py
│   ├── confluence_scorer.py
│   ├── market_correlation.py # IMOEX beta, relative strength
│   └── orderbook_analysis.py
│
├── journal/
│   ├── snapshots.py           # MarketSnapshot, DecisionSnapshot, ReasoningStep
│   ├── trade_review.py        # MFE/MAE, exit_efficiency, post_exit_drift
│   ├── hypothesis_engine.py   # Hypothesis, статистическая проверка
│   └── advisory.py            # генерация текстовых советов из подтверждённых гипотез
│
└── ports/
    ├── market_data.py    # MarketDataPort
    ├── broker.py         # OrderExecutionPort
    ├── persistence.py    # RepositoryPort
    ├── archive.py        # ArchivePort
    ├── clock.py          # ClockPort
    └── notifier.py       # NotificationPort
```

---

## 4. Доменные сущности — точные контракты

### 4.1. `domain/enums.py` — единая точка правды по статусам

```python
class Timeframe(StrEnum):
    D1 = "1d"
    H1 = "1h"
    M1 = "1m"

class Trend(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"

class MarketRegime(StrEnum):
    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"

class TradePlanStatus(StrEnum):
    PROPOSED = "proposed"     # сформирован, ещё не подан на исполнение
    PENDING = "pending"       # ордер выставлен, ждём заполнения
    ACTIVE = "active"         # позиция открыта
    CLOSED_TARGET = "closed_target"
    CLOSED_HARD_STOP = "closed_hard_stop"
    CLOSED_INVALIDATION = "closed_invalidation"
    CLOSED_TIME_EXIT = "closed_time_exit"
    REJECTED = "rejected"     # риск-модуль отклонил до исполнения

class DecisionType(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    REJECT = "reject"

class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    APPLIED = "applied"

class TradeVerdict(StrEnum):
    GOOD_EXIT = "good_exit"
    PREMATURE_EXIT = "premature_exit"     # ваш кейс "закрыл в 10, а разлетелось до 25"
    OVERSTAYED = "overstayed"             # держал слишком долго, отдал прибыль
    CORRECT_CAUTION = "correct_caution"   # низкий MFE, выход всё равно был верным
```

Явное разделение `CLOSED_*` статусов (а не единый `CLOSED` + отдельное поле "причина") — намеренное решение: это даёт возможность фильтровать сделки по причине закрытия прямо в SQL при дата-майнинге без JOIN на дополнительную таблицу.

### 4.2. `domain/value_objects.py`

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Money:
    amount: Decimal
    currency: str = "RUB"

@dataclass(frozen=True, slots=True, kw_only=True)
class Percentage:
    value: Decimal   # 0.006 == 0.6%, никогда не хранить как "0.6"

@dataclass(frozen=True, slots=True, kw_only=True)
class OHLCV:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    timestamp: datetime
    timeframe: Timeframe

@dataclass(frozen=True, slots=True, kw_only=True)
class OrderbookLevel:
    price: Decimal
    quantity: int

@dataclass(frozen=True, slots=True, kw_only=True)
class OrderbookSnapshot:
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    captured_at: datetime

    @property
    def spread(self) -> Decimal:
        return self.asks[0].price - self.bids[0].price

    @property
    def imbalance(self) -> Decimal:
        bid_vol = sum(l.quantity for l in self.bids)
        ask_vol = sum(l.quantity for l in self.asks)
        return Decimal(bid_vol - ask_vol) / Decimal(bid_vol + ask_vol)
```

Именование: `*_at` — всегда `datetime` момента события (`captured_at`, `created_at`, `closed_at`), `*_pct` — всегда `Decimal`-доля от единицы (не 0-100!), `*_price` — денежная величина конкретного уровня, `*_amount`/`money` поля — обёрнуты в `Money`, если участвуют в расчётах P&L.

### 4.3. `domain/entities.py`

```python
@dataclass(slots=True, kw_only=True)
class Instrument:
    uid: str                    # instrument_uid из API — первичный ключ везде
    ticker: str
    lot_size: int
    is_benchmark: bool = False  # True только для IMOEX

@dataclass(slots=True, kw_only=True)
class ReasoningStep:
    module: str          # "regime_detector", "fibonacci", "orderbook_analysis"
    signal: str          # "trend_up", "near_618_retracement", "bid_heavy"
    weight: Decimal      # вклад в confluence_score, может быть отрицательным
    raw_value: Decimal | None = None

@dataclass(slots=True, kw_only=True)
class TradeThesis:
    reasoning_chain: tuple[ReasoningStep, ...]
    confluence_score: Decimal
    timeframe_bias: dict[Timeframe, Trend]

@dataclass(slots=True, kw_only=True)
class InvalidationRule:
    description: str                 # человекочитаемое условие для GUI
    check: Callable[["MarketSnapshot"], bool]  # True == тезис инвалидирован

@dataclass(slots=True, kw_only=True)
class TradePlan:
    id: UUID
    instrument: Instrument
    entry_price: Decimal
    hard_stop_price: Decimal
    target_price: Decimal
    thesis: TradeThesis
    thesis_invalidation: InvalidationRule
    max_holding_time: timedelta
    created_at: datetime
    status: TradePlanStatus = TradePlanStatus.PROPOSED

@dataclass(slots=True, kw_only=True)
class Position:
    instrument: Instrument
    quantity: int
    average_entry: Decimal
    opened_at: datetime
    linked_plan_id: UUID
```

Правило именования: суффикс `_id` — всегда `UUID`, генерируемый в момент создания сущности в `core` (не в БД через `AUTOINCREMENT`!) — это гарантирует, что `TradePlan.id` существует и стабилен ещё до первой записи в репозиторий, что важно для связывания `MarketSnapshot`/`DecisionSnapshot` до персистентности.

---

## 5. Порты — точные контракты (`core/ports/`)

### 5.1. `market_data.py`

```python
class MarketDataPort(Protocol):
    async def get_candles(
        self, instrument: Instrument, timeframe: Timeframe,
        from_: datetime, to: datetime,
    ) -> list[OHLCV]: ...

    async def stream_candles(
        self, instrument: Instrument, timeframe: Timeframe,
    ) -> AsyncIterator[OHLCV]: ...

    async def get_orderbook(
        self, instrument: Instrument, depth: int = 20,
    ) -> OrderbookSnapshot: ...

    async def get_api_indicator(
        self, instrument: Instrument, indicator: str,
        timeframe: Timeframe, params: dict[str, Any],
    ) -> Decimal: ...
```

### 5.2. `broker.py`

```python
class OrderExecutionPort(Protocol):
    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult: ...
    async def cancel_order(self, order_id: str) -> None: ...
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
```

Обязательное требование ко всем реализациям: любой вызов обязан внутри адаптера проверять `account_id == settings.managed_account_id` — но сам контракт этого не типизирует, это ответственность адаптера (ядро лишь передаёт `Instrument`/`TradePlan`, не зная об account_id вообще — это деталь инфраструктуры).

### 5.3. `persistence.py` — единый порт для hot/warm/cold, деталь реализации скрыта

```python
class RepositoryPort(Protocol):
    async def save_market_snapshot(self, snapshot: MarketSnapshot) -> UUID: ...
    async def save_decision_snapshot(self, snapshot: DecisionSnapshot) -> UUID: ...
    async def save_trade_plan(self, plan: TradePlan) -> None: ...
    async def get_trade_history(
        self, instrument: Instrument | None, since: datetime,
    ) -> list[TradeReview]: ...
    async def save_hypothesis(self, hypothesis: Hypothesis) -> None: ...
```

Ядро не знает о существовании DuckDB/Parquet/hot-warm-cold — это чисто адаптерная деталь. Единственное, что ядру нужно — гарантия персистентности через один интерфейс.

### 5.4. `clock.py`

```python
class ClockPort(Protocol):
    def now(self) -> datetime: ...
```

Любой код в `core/`, которому нужно текущее время (TTL-проверки, `max_holding_time`), обязан принимать `ClockPort` через конструктор/аргумент, а не звать `datetime.now()` напрямую — это единственный способ детерминированно тестировать стратегию и прогонять backtest на исторических датах без искажения "текущим временем машины".

---

## 6. Индикаторы — единый протокол для расширяемости (ваша ключевая "фишка" п.8)

Чтобы "добавление новой самописной ТА" было буквально добавлением одного файла без правок остального кода, все индикаторы в `core/analysis/` подчиняются одному протоколу:

```python
# core/analysis/protocols.py
class Indicator(Protocol):
    name: str  # уникальный ключ, под которым живёт в indicators[timeframe][name]

    def calculate(self, candles: list[OHLCV]) -> Decimal: ...
    def required_lookback(self) -> int: ...  # сколько свечей назад нужно для расчёта
```

Регистрация новых индикаторов — через простой реестр (`IndicatorRegistry`), который `application/` опрашивает при сборке `MarketSnapshot`. Никакого наследования, никакого редактирования `run_analysis_cycle` при добавлении нового индикатора — только регистрация новой реализации `Indicator`.

`confluence_scorer.py` работает поверх результатов всех зарегистрированных индикаторов сразу, не зная заранее их конкретного набора — принимает `dict[str, Decimal]` и веса из конфига (передаются снаружи, не хардкодятся).

---

## 7. Журнал и самоанализ — точные структуры данных

### 7.1. `journal/snapshots.py`

```python
@dataclass(slots=True, kw_only=True)
class MarketSnapshot:
    id: UUID
    instrument_uid: str
    captured_at: datetime
    ohlcv: dict[Timeframe, OHLCV]
    indicators: dict[Timeframe, dict[str, Decimal]]
    orderbook: OrderbookSnapshot | None
    imoex_correlation: Decimal | None
    market_regime: dict[Timeframe, MarketRegime]

@dataclass(slots=True, kw_only=True)
class DecisionSnapshot:
    id: UUID
    market_snapshot_id: UUID
    trade_plan_id: UUID | None
    decision: DecisionType
    reasoning_chain: tuple[ReasoningStep, ...]
    confluence_score: Decimal
    risk_check_passed: bool
    risk_check_reason: str | None
    thought_text: str          # человекочитаемая мысль для GUI
    created_at: datetime
```

### 7.2. `journal/trade_review.py`

```python
@dataclass(slots=True, kw_only=True)
class TradeReview:
    trade_plan_id: UUID
    entry_price: Decimal
    exit_price: Decimal
    mfe: Decimal
    mae: Decimal
    exit_efficiency: Decimal
    price_at_session_close: Decimal
    price_at_t_plus_1d: Decimal | None
    price_at_t_plus_3d: Decimal | None
    post_exit_drift_pct: Decimal
    verdict: TradeVerdict
```

### 7.3. `journal/hypothesis_engine.py`

```python
@dataclass(slots=True, kw_only=True)
class Hypothesis:
    id: UUID
    text: str
    condition_description: str
    sample_size: int
    confidence: Decimal
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    suggested_action: str
    walk_forward_efficiency: Decimal | None = None  # заполняется на этапе TESTING
```

Минимальный `sample_size` для перехода из `PROPOSED` в `TESTING` — константа конфигурации (не хардкод в ядре, а параметр, передаваемый в `hypothesis_engine` снаружи), по умолчанию 30, как зафиксировано ранее.

---

## 8. Движение данных внутри ядра (без внешних деталей)

```
MarketDataPort.get_candles()/stream_candles()
        │  (данные приходят ИЗВНЕ через порт, ядро не знает откуда)
        ▼
[индикаторы из core/analysis/*] → indicators: dict[Timeframe, dict[str, Decimal]]
        │
        ▼
[regime_detector] → MarketRegime по каждому фрейму
        │
        ▼
MarketSnapshot (собран полностью, ещё не сохранён)
        │
        ▼
[setup_scanner + entry_timing + confluence_scorer] → TradeThesis
        │
        ▼
[cost_model.min_viable_target_pct] — фильтр издержек
        │
        ▼ (прошёл фильтр)
[position_sizing] → количество лотов
        │
        ▼
TradePlan (status=PROPOSED)
        │
        ▼
DecisionSnapshot (decision=ENTER, привязан к MarketSnapshot.id и TradePlan.id)
        │
        ▼
  (дальше — ответственность application/, ядро отдаёт TradePlan наружу)
```

Важно: ядро **само не сохраняет** снапшоты — оно их только формирует и возвращает как значения (immutable dataclasses). Вызов `RepositoryPort.save_*` происходит в `application/use_cases/`, а не внутри `core/`. Это разделение защищает ядро от знания о персистентности вообще — ядро мыслит только в терминах "вычислить и вернуть", что делает его тривиально тестируемым без моков БД.

---

## 9. Соглашения об именовании (сводная таблица)

| Категория | Правило | Пример |
|---|---|---|
| Классы (Entity/VO) | `PascalCase`, существительное | `TradePlan`, `OrderbookSnapshot` |
| Enum-классы | `PascalCase`, оканчиваются на роль, не на `Enum` | `TradePlanStatus`, не `StatusEnum` |
| Enum-члены | `UPPER_CASE` | `MarketRegime.TRENDING` |
| Поля-время | суффикс `_at`, тип `datetime` | `created_at`, `captured_at` |
| Поля-доли/проценты | суффикс `_pct`, `Decimal` в диапазоне 0–1 | `post_exit_drift_pct` |
| Поля-деньги | суффикс `_price`/`_amount` либо тип `Money` | `hard_stop_price` |
| Поля-идентификаторы | суффикс `_id`, тип `UUID` (кроме `instrument.uid` — внешний ключ API) | `market_snapshot_id` |
| Protocol-порты | суффикс `Port` | `MarketDataPort` |
| Индикаторы | модуль = имя индикатора в snake_case, класс = PascalCase + `Indicator` | `atr.py` → `AtrIndicator` |
| Юзкейсы (вне core, для справки) | глагол + существительное, snake_case файл | `run_analysis_cycle.py` |

---

## 10. Тестирование ядра

`tests/unit/` покрывает `core/` **без единого сетевого вызова или обращения к диску** — все порты в тестах заменяются на простые фейки (in-memory реализации `Protocol`, буквально классы с тем же набором методов, без библиотек мокирования, за счёт структурной типизации). `FrozenClock` из `ClockPort` — обязательный инструмент для тестирования TTL-логики (`max_holding_time`, `time_exit`) детерминированно.

---

## Резюме: почему это решает исходные задачи

- **Лёгкая интеграция новых модулей (гл. требование)** — новый индикатор = новый файл + запись в реестре; новый порт добавляется только когда действительно нужен второй адаптер; ядро не меняется при добавлении адаптеров.
- **MFE/MAE/post-exit-drift и самоанализ (п.1 прошлого сообщения)** — формализованы как точные dataclass-структуры с версионированием статуса гипотез, что исключает "магию" в коде.
- **Максимально подробные снапшоты (п.3)** — `MarketSnapshot`/`DecisionSnapshot` спроектированы как immutable value objects с полным деревом данных, готовые к сериализации в любой формат хранения адаптером, без изменения структуры домена.
- **Отсутствие фанатизма** — 6 портов на весь проект, ядро остаётся компактным и читаемым, несмотря на полноту доменной модели.