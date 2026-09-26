# 05. Red-Bot-Investment — синтез и целевая архитектура

Этот документ сводит выводы `01`–`04` в план и содержит ранние архитектурные эскизы. Для фактической конфигурации, которая изменилась после реализации UI/CLI настроек, нормативны `plan/config.md` и `README.md`: режим и счёт сохраняются в DuckDB, `.env` для них не нужен.

---

## 5.1. Принципы (не обсуждаются)

1. **Риск-инфраструктура раньше исполнения.** Лимиты, kill switch и реконсиляция
   пишутся до того, как первая живая заявка уйдёт на биржу.
2. **Стратегия не торгует.** Она возвращает намерение. Решение принимает
   `RiskManager`, отправляет `OrderExecutor`.
3. **Деньги — только `Decimal`.** На границе API — `Quotation`/`MoneyValue` через
   `t_tech.invest.utils`. Никакого `float` в расчётах заявок.
4. **Каждая мутация идемпотентна.** Клиентский ключ сохраняется **до** сетевого
   вызова.
5. **Режим задаётся оператором.** `backtest` / `sandbox` / `live` — типизированное
   значение; default хранится в DuckDB и меняется через GUI/CLI, а не зависит от данных,
   промта или ответа модели. Новый режим применяется после рестарта.
6. **Источник истины по заявкам — брокер.** Внутреннее состояние всегда
   сверяется с ним.
7. **Легаси не тащим.** Только `t_tech.invest.grpc`, только request-модели.

---

## 5.2. Компоненты

```
                   ┌──────────────────────────────────────────┐
  T-Invest API ───▶│ MarketDataProvider                       │
   (gRPC)          │  • стримы: свечи/стакан/сделки/статусы    │
                   │  • unary: свечи, инструменты, портфель    │
                   │  • нормализация в Decimal, UTC, лоты      │
                   └──────────────────┬───────────────────────┘
                                      ▼
                   ┌──────────────────────────────────────────┐
                   │ Strategy                                 │
                   │  • чистые функции: данные → сигнал        │
                   │  • без сети, без post_order               │
                   │  • выдаёт OrderIntent (структура)         │
                   └──────────────────┬───────────────────────┘
                                      ▼
                   ┌──────────────────────────────────────────┐
                   │ RiskManager (детерминированный)          │
                   │  • pre-trade чек-лист                    │
                   │  • лимиты: заявка, позиция, дневной убыток│
                   │  • сайзинг: (Капитал × Риск%)/стоп, лоты │
                   │  • вердикт: approve / reject + причина    │
                   └──────────────────┬───────────────────────┘
                                      ▼
                   ┌──────────────────────────────────────────┐
                   │ OrderExecutor                            │
                   │  • client_key = uuid7() → StateStore      │
                   │  • сборка PostOrderRequest                 │
                   │  • отправка, обработка неопределённости    │
                   │  • сохранение exchange_id и request_id     │
                   └──────────────────┬───────────────────────┘
                                      ▼
                                  T-Invest API

  Отдельный процесс:
   ┌───────────────────────────────────────────────────────────┐
   │ Supervisor: kill switch + Reconciler + алерты             │
   │  • не зависит от падения стратегии                        │
   │  • сверка: наши заявки/позиции ↔ брокер                    │
   │  • при расхождении или лимите: stop → cancel → flatten     │
   └───────────────────────────────────────────────────────────┘
```

### Границы и контракты

| Компонент | Вход | Выход | Запрещено |
| :-- | :-- | :-- | :-- |
| `MarketDataProvider` | id инструментов, интервалы | нормализованные свечи/стакан/статусы | принимать торговые решения |
| `Strategy` | история + состояние | `OrderIntent` (или `None`) | сеть, обращение к SDK, `post_order` |
| `RiskManager` | `OrderIntent` + лимиты + счёт | `Decision(approved, lots, price, reason)` | недетерминизм, randomness без seed |
| `OrderExecutor` | `Decision` | результат отправки + id | менять решение риск-менеджера |
| `StateStore` | события | персистентное состояние | быть единственным источником истины о позициях |
| `Supervisor` | брокер + состояние | команды остановки | жить в том же процессе, что стратегия |

### Типы домена (эскиз)

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"  # sandbox
    LIVE = "live"  # production


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Instrument:
    uid: str
    ticker: str
    class_code: str
    lot: int
    min_price_increment: Decimal
    currency: str

    def round_price(self, price: Decimal) -> Decimal:
        ticks = (price / self.min_price_increment).to_integral_value()
        return ticks * self.min_price_increment


@dataclass(frozen=True, slots=True)
class OrderIntent:
    instrument: Instrument
    side: Side
    stop_price: Decimal
    signal_strength: float  # не используется как размер позиции!
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    lots: int
    limit_price: Decimal | None
    reason: str

    @classmethod
    def reject(cls, reason: str) -> Self:
        return cls(approved=False, lots=0, limit_price=None, reason=reason)
```

---

## 5.3. Конфигурация

Одна типизированная модель, валидируется на старте, без секретов в логах.

```python
# src/red_bot/config.py (эскиз)
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from red_bot.domain import Mode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",  # optional advanced overrides; no .env required for normal use
        env_prefix="RED_BOT_",
    )

    mode: Mode = Mode.SANDBOX  # fallback; saved user default lives in DuckDB
    token: SecretStr
    account_id: str = ""  # optional explicit override; otherwise resolve OPEN account from API

    # риск
    risk_per_trade_pct: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.02"))
    max_lots_per_order: int = 10
    max_position_lots: int = 50
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_orders_per_minute: int = 15  # лимит postOrder — 15/сек, но держим запас
    max_notional_per_order: Decimal = Decimal("1_000_000")

    # торговля
    instruments: tuple[str, ...] = ("T_TQBR",)
    trade_window_start: str = "10:00"
    trade_window_end: str = "18:40"
    allow_auctions: bool = False

    # издержки (в бэктесте и пре-трейд оценке)
    broker_fee_pct: Decimal = Decimal("0.0005")
    slippage_ticks: int = 1
    tax_rate: Decimal = Decimal("0.13")
```

Правила:

- `mode` — enum, ни в коем случае не строка из входных данных.
- `token` — `SecretStr`, только из env.
- Лимиты задаются **вне** стратегии и не могут быть изменены ей в рантайме.

---

## 5.4. Ключевые алгоритмы

### Сайзинг с учётом лотности MOEX

```python
from decimal import ROUND_DOWN, Decimal


def size_position(
    equity: Decimal,
    risk_pct: Decimal,
    entry: Decimal,
    stop: Decimal,
    instrument: Instrument,
    max_lots: int,
) -> int:
    """Возвращает количество ЛОТОВ. Никогда не округляем вверх."""
    risk_money = equity * risk_pct
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return 0
    units = (risk_money / risk_per_unit).to_integral_value(rounding=ROUND_DOWN)
    lots = int((units / instrument.lot).to_integral_value(rounding=ROUND_DOWN))
    return max(0, min(lots, max_lots))
```

### Pre-trade чек-лист (детерминированный)

```python
def check(intent, ctx) -> Decision:
    # 1. режим и счёт
    # 2. инструмент найден однозначно (ровно один uid)
    # 3. торговый статус разрешает тип заявки (см. таблицу статусов)
    # 4. api_trade_available_flag / buy_available_flag / sell_available_flag
    # 5. количество лотов > 0, цена > 0 и кратна шагу
    # 6. доступные средства / позиция
    # 7. лимиты: заявка, позиция, нотионал, дневной убыток, частота
    # 8. торговое окно (не аукцион, если allow_auctions=False)
    # 9. тип заявки и все обязательные поля request-модели
    # 10. в логах нет токена
```

### Протокол отправки

```python
async def execute(decision, executor, store) -> None:
    client_key = store.create_client_key(decision)  # uuid7, ПИШЕМ ДО СЕТИ
    request = executor.build_request(decision, client_key)
    try:
        response = await executor.send(request)
    except TimeoutError, ConnectionError:
        # НЕ создаём новый ключ, НЕ повторяем
        await store.mark_unknown(client_key)
        await executor.reconcile(client_key)  # getOrderState по ключу
        return
    await store.bind(client_key, response.order_id, response.order_request_id)
```

### Реконсиляция (Supervisor, раз в N минут + после реконнекта)

1. Запросить у брокера открытые заявки, позиции, последние операции.
2. Сравнить с `StateStore`.
3. Любое расхождение → алерт + остановка новых сигналов.
4. Критическое расхождение (позиция отличается) → kill-последовательность:
   отозвать право отправки → отменить открытые заявки (по данным брокера) →
   закрыть позицию → **проверить, что позиция нулевая** → ждать человека.

---

## 5.5. Технологический стек

| Слой | Выбор |
| :-- | :-- |
| Python | 3.14 (3.15 — после 2026-10-01) |
| Окружение / deps | `uv`, lock-файл в git, кастомный индекс Т-Банка для `t-tech-investments` |
| SDK | `t-tech-investments` 1.51.x, только `t_tech.invest.grpc` |
| Линт/формат | `ruff` (`E,F,W,I,N,UP,B,C4,SIM,ASYNC,RUF`) |
| Типы | `mypy --strict` в CI, `pyright` в редакторе |
| Тесты | `pytest`, `pytest-asyncio`; маркер `test_sandbox` для сетевых тестов |
| Хуки | `pre-commit` |
| Конфиг | `pydantic-settings` |
| Логи | `structlog` (JSON в проде), t-строки для сообщение+поля |
| Хранение истории | SQLite/Parquet + `compression.zstd` (3.14) |
| Бэктест | свой движок на `polars`/`pandas` + `numpy`; метрики и CPCV/WFO — свои |
| Состояние | SQLite (WAL), таблицы: `orders`, `intents`, `decisions`, `positions_snapshot`, `reconciliation_log` |
| Метрики/алерты | минимум — структурированные логи + heartbeat; затем Prometheus/Telegram |

---

## 5.6. Дорожная карта

### Этап 0. Каркас (1–2 дня)

- `pyproject.toml` (PEP 621, `requires-python = ">=3.14"`, ruff/mypy/pytest, uv index),
  `uv.lock`, `.python-version`, `.pre-commit-config.yaml`, `.gitignore`, `src/` layout.
- `AGENTS.md` (уже есть), `README.md` (уже есть).
- CI: `uv sync` → `ruff check` → `ruff format --check` → `mypy` → `pytest`.

**Готово:** зелёный CI на пустом проекте.

### Этап 1. Домен и конверсии (2–3 дня)

- `domain/`: `Instrument`, `Candle`, `OrderIntent`, `Decision`, `Money`.
- `broker/converters.py`: обёртки над `quotation_to_decimal`, `decimal_to_quotation`,
  `money_to_decimal`, `decimal_to_money`.
- Тесты: округление до шага цены, формулы стоимости для акций/облигаций/валюты/
  фьючерсов, расчёт лотов, НКД.

**Готово:** 100 % покрытие денежной арифметики тестами, ни одного `float`.

### Этап 2. Брокер-адаптер и данные (3–5 дней)

- `broker/client.py`: фабрика клиента по `Mode` (`Client` / `SandboxClient` /
  `AsyncClient` / `AsyncSandboxClient`), контекстный менеджер, retry только для
  read-only.
- `data/instruments.py`: кэш инструментов ( uid / ticker / class_code / lot /
  min_price_increment ) с TTL и с учётом лимита 15 запросов/мин на справочники.
- `data/history.py`: загрузка свечей с чанкованием по таблице интервалов;
  сохранение в Parquet + zstd.
- `data/streams.py`: стрим свечей/стакана с переподключением и контролем
  «успеваем вычитывать».
- Тесты: на моках, без сети.

**Готово:** робот умеет выгрузить год дневных свечей и держать стрим 30 минут без
разрывов.

### Этап 3. Бэктест (5–7 дней)

- Движок событийного бэктеста: комиссия, спред, проскальзывание, НКД, налог.
- Метрики: expectancy (R), PF, Sharpe, Sortino, Calmar, MaxDD, recovery factor,
  серия убытков.
- Защита от подгонки: hold-out открывается один раз, walk-forward (4:1),
  purged k-fold + embargo, DSR, PBO.
- Отчёт в markdown с графиками.

**Готово:** одна стратегия-бенчмарк (например, кросс скользящих средних с
ATR-стопом) имеет воспроизводимый отчёт с DSR/PBO.

### Этап 4. Риск и исполнение (4–6 дней)

- `RiskManager` с полным pre-trade чек-листом.
- `StateStore` на SQLite: клиентские ключи, биржевые id, состояния, решения.
- `OrderExecutor` с протоколом идемпотентности и обработкой `30057`, `30059`.
- `Supervisor`: kill switch + реконсиляция, **отдельный процесс**.
- Тесты: таймаут, повторный сигнал, частичное исполнение, отмена, восстановление
  после перезапуска, запрет мутации в read-only.

**Готово:** chaos-тесты (fault injection, флуд заявками) проходят — бот
останавливается, а не торгует.

### Этап 5. Песочница (1–2 недели календарных)

- Прогон в `SandboxClient` на реальных данных.
- Проверка: реконнекты стримов, сверка позиций, обработка аукционов и
  вечерней сессии, лимиты API не пробиваются.
- Сверка результатов песочницы с бэктестом: расхождение объяснено (комиссия
  0,05 %, отсутствие налогов и проскальзывания).

**Готово:** 10 торговых дней без ручного вмешательства и без расхождений в
реконсиляции.

### Этап 6. Боевой запуск

- Минимальный капитал, `mode = LIVE`, лимиты занижены вдвое.
- Ежедневный отчёт: сделки, PnL, просадка, расхождения реконсиляции, срабатывания
  лимитов.
- Масштабирование — только после 100+ реальных сделок, совпадающих с
  ожиданиями бэктеста по распределению R.

---

## 5.7. Что сознательно НЕ делаем на старте

| Не делаем | Почему |
| :-- | :-- |
| HFT и арбитраж | критичны к задержкам, нужен colocation; API Т-Инвестиций для этого не предназначен |
| Маржинальная торговля | сильно замедляет исполнение и добавляет риск маржин-колла |
| Шорты как основной режим | риск неограниченной потери, отдельный класс проверок |
| Опционы | FIGI не поддерживается, нужна отдельная модель ГО и экспирации |
| LLM в контуре принятия решения | сначала детерминированная стратегия; LLM — только как исследовательский инструмент вне рантайма |
| Мультиаккаунт и распараллеливание потоков | нужно только при десятках тысяч заявок в сутки |
| Free-threading | упор в сеть, не в GIL |

---

## 5.8. Определение «готово»

Бот считается рабочим, когда одновременно выполняется:

- [ ] CI зелёный: ruff, mypy strict, pytest (unit, без сети).
- [ ] Нет ни одного `float` в денежных расчётах (проверяется grep-правилом в CI).
- [ ] Токен только в env; в git и логах его нет.
- [ ] У каждой отправленной заявки есть сохранённый клиентский ключ.
- [ ] Kill switch останавливает торговлю при убитом процессе стратегии.
- [ ] Реконсиляция расхождений — 0 за последние 10 торговых дней.
- [ ] Бэктест и реальность совпадают по распределению R в пределах допустимой
      погрешности.
