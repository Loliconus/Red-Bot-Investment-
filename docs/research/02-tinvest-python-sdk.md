# 02. T-Invest Python SDK (`invest-python`) — полный разбор

Источник: [opensource.tbank.ru/invest/invest-python](https://opensource.tbank.ru/invest/invest-python),
ветка `master`, версия **1.51.0** (актуально на 2026-09-24).

---

## 2.1. Установка

Пакет называется **`t-tech-investments`** и публикуется в GitLab-реестре Т-Банка,
а не на PyPI:

```bash
pip install t-tech-investments \
  --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```

Нюансы установки:

- Для валидации сертификата нужны **сертификаты МинЦифры** (для РФ). Без них —
  `--trusted-host opensource.tbank.ru`.
- Если возникает ошибка валидации SSL при установлении соединения — задать
  переменную окружения `SSL_TBANK_VERIFY=True`: тогда библиотека использует
  поставляемый с ней сертификат МинЦифры.
- Для `uv` индекс задаётся в `pyproject.toml`:

  ```toml
  [[tool.uv.index]]
  name = "tbank"
  url = "https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
  explicit = true

  [tool.uv.sources]
  t-tech-investments = { index = "tbank" }
  ```

- Лицензия: Apache-2.0. Репозиторий позиционируется как продукт независимой
  разработки.

Возможности SDK: синхронный и асинхронный gRPC-клиент, отмена всех заявок,
выгрузка истории котировок «от» и «до», кеширование данных, готовая торговая
стратегия.

---

## 2.2. История неймспейсов и breaking changes

| Версия | Что случилось |
| :-- | :-- |
| `0.2.0-beta*` | ранние версии, пакет `tinkoff-investments` |
| `0.3.0` | **пакет переименован в `t_tech`** со всеми подпакетами |
| `1.0.0` | **кодогенерация из proto** + request-обёртки. Старые сервисы из `t_tech.invest.services` помечены deprecated и не развиваются |
| `1.51.0` | текущая версия |

### Миграция на 1.0.0

```python
# было
from t_tech.invest import ...
from t_tech.invest.services import ...
from t_tech.invest.schemas import ...

# стало
from t_tech.invest.grpc import ...
from t_tech.invest.grpc.schemas import ...
```

Аргументы методов обёрнуты в request-модели (имена внутренних полей сохранены):

| Было | Стало |
| :-- | :-- |
| `client.orders.post_order(...)` | `client.orders.post_order(request=PostOrderRequest(...))` |
| `client.orders.cancel_order(...)` | `client.orders.cancel_order(request=CancelOrderRequest(...))` |
| `client.orders.get_order_state(...)` | `client.orders.get_order_state(request=GetOrderStateRequest(...))` |
| `client.orders.get_orders(account_id=...)` | `client.orders.get_orders(request=GetOrdersRequest(account_id=..., ...))` — `account_id` уехал внутрь `request`, остальные фильтры — в `GetOrdersRequestFilters` |
| `client.operations.get_operations` | `...request=OperationsRequest(...)` |
| `client.operations.get_positions` | `...request=PositionsRequest(...)` |
| `client.operations.get_portfolio` | `...request=PortfolioRequest(...)` |
| `client.operations_stream.portfolio_stream` | `...request=PortfolioStreamRequest(...)` |
| `client.operations_stream.positions_stream` | `...request=PositionsStreamRequest(...)` |
| `client.orders_stream.trade_stream` | `...request=TradeStreamRequest(...)` |

> **Практический вывод:** любой сниппет из блогов 2023–2024 гг. нужно
> перепроверять по текущей версии пакета. Команда проверки перед написанием
> вызова:
>
> ```bash
> rg -n "def post_order|class PostOrderRequest" $(python -c "import t_tech, os; print(os.path.dirname(t_tech.__file__))")/invest/grpc
> ```

---

## 2.3. Клиенты

| Клиент | Выполнение | Контур |
| :-- | :-- | :-- |
| `Client` | синхронное | реальный по умолчанию |
| `AsyncClient` | асинхронное | реальный по умолчанию |
| `SandboxClient` | синхронное | песочница |
| `AsyncSandboxClient` | асинхронное | песочница |

Определения — в `t_tech/invest/grpc/utils/clients.py`.

Контекстный менеджер возвращает **набор сервисов**:

```python
import os

from t_tech.invest.grpc import Client
from t_tech.invest.grpc.schemas import GetAccountsRequest

token = os.environ["INVEST_TOKEN"]

with Client(token) as services:
    response = services.users.get_accounts(request=GetAccountsRequest())
    print(response.accounts)
```

Асинхронный вариант:

```python
import asyncio
import os

from t_tech.invest.grpc import AsyncClient
from t_tech.invest.grpc.schemas import GetAccountsRequest


async def main() -> None:
    token = os.environ["INVEST_TOKEN"]
    async with AsyncClient(token) as services:
        response = await services.users.get_accounts(request=GetAccountsRequest())
        print(response.accounts)


asyncio.run(main())
```

Переопределение контура вручную (устаревший стиль, но встречается):

```python
from t_tech.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX

with Client(token, target=INVEST_GRPC_API) as services:
    ...
```

Новый код должен просто выбирать нужный класс клиента.

---

## 2.4. Сервисы

| Атрибут | Назначение |
| :-- | :-- |
| `services.users` | счета и пользовательские параметры |
| `services.instruments` | поиск инструментов и их свойства |
| `services.market_data` | свечи, цены, стаканы, торговые статусы |
| `services.market_data_stream` | поток рыночных данных |
| `services.operations` | портфель, позиции, операции |
| `services.operations_stream` | поток изменений портфеля и позиций |
| `services.orders` | обычные биржевые заявки |
| `services.orders_stream` | поток состояния заявок и сделок |
| `services.stop_orders` | стоп-заявки |
| `services.sandbox` | специальные операции песочницы |
| `services.signals` | сигнальные методы API |

Точный состав — `t_tech/invest/grpc/utils/grpc_services.py`.

Общий вид вызова:

```python
response = services.some_service.some_method(
    request=SomeMethodRequest(
        # поля — только из определения модели текущей версии
    )
)
```

---

## 2.5. Финансовые преобразования

Публичные функции — в `t_tech.invest.utils`:

```python
from t_tech.invest.utils import (
    decimal_to_money,
    decimal_to_quotation,
    money_to_decimal,
    quotation_to_decimal,
)
```

- `quotation_to_decimal` / `decimal_to_quotation` — для `Quotation`;
- `money_to_decimal` / `decimal_to_money` — для `MoneyValue`; `decimal_to_money`
  **принимает код валюты** согласно текущей сигнатуре.

Правила:

- конвертируем **только** этими функциями;
- не собираем `units`/`nano` руками;
- не используем `float` в расчётах заявок;
- нормализуем цену к шагу цены **до** создания request-модели.

Пример корректной подготовки цены:

```python
from decimal import ROUND_HALF_UP, Decimal

from t_tech.invest.utils import decimal_to_quotation


def normalize_price(price: Decimal, step: Decimal) -> Decimal:
    """Приводит цену к шагу цены инструмента."""
    ticks = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return ticks * step


price_q = decimal_to_quotation(normalize_price(Decimal("275.13"), Decimal("0.01")))
```

---

## 2.6. Идентификаторы заявки

Три похожих имени с разным смыслом:

| Поле | Смысл |
| :-- | :-- |
| `PostOrderRequest.order_id` | **клиентский ключ идемпотентности**, создаём мы |
| `PostOrderResponse.order_id` | **биржевой** идентификатор заявки |
| `PostOrderResponse.order_request_id` | возвращённый клиентский ключ |

Запрос состояния:

```python
from t_tech.invest.grpc.schemas import GetOrderStateRequest

# биржевой id
state = services.orders.get_order_state(
    request=GetOrderStateRequest(
        account_id=account_id,
        order_id=exchange_order_id,
        order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
    )
)

# клиентский ключ идемпотентности
state = services.orders.get_order_state(
    request=GetOrderStateRequest(
        account_id=account_id,
        order_id=client_key,
        order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
    )
)
```

Перед использованием сверить поля и enum с текущей версией моделей.

---

## 2.7. Каноны из `AGENTS.md` репозитория SDK

Официальные требования к агентам, работающим с SDK. Формулировки сохранены
по смыслу:

**Проект и источники истины**

- Поддерживаемая версия Python — в `pyproject.toml`; актуальный интерфейс —
  `t_tech.invest.grpc`.
- Приоритет фактов об API: Python-сигнатуры и proto-контракты → тесты текущей
  версии → `BREAKING_CHANGES.md` → документация и примеры репозитория → внешние
  материалы.
- Не выдумывать классы, enum, поля и методы — искать определение в текущей
  версии репозитория.

**Карта репозитория**

| Путь | Что там |
| :-- | :-- |
| `t_tech/invest/grpc/` | актуальные модели, сервисы и клиенты gRPC |
| `protos/t_tech/invest/grpc/` | исходные proto-контракты |
| `t_tech/invest/grpc/utils/clients.py` | `Client`, `AsyncClient`, `SandboxClient`, `AsyncSandboxClient` |
| `examples/` | пользовательские сценарии |
| `tests/` | unit- и интеграционные тесты |
| `docs/` | документация MkDocs |
| `.agents/skills/t-invest-python-sdk-bot-builder/` | workflow разработки и ревью торговых программ |
| `scripts/generate_models/` | генерация Python-моделей и сервисов |
| `scripts/noxfile.py` | тестовая матрица версий Python |

**Соглашения**

- Новый код — импорты из `t_tech.invest.grpc`; request-модели — из
  `t_tech.invest.grpc.schemas`.
- Не использовать `tinkoff.invest`, не добавлять код на `t_tech.invest.services`.
- Клиент — контекстный менеджер.
- Не смешивать sync и async: все вызовы `AsyncClient` должны быть `await`-нуты.
- Сохранять совместимость с минимальной версией Python из `pyproject.toml`.

**Контуры**

- `Client`/`AsyncClient` по умолчанию — реальный endpoint;
  `SandboxClient`/`AsyncSandboxClient` — sandbox.
- Не менять выбранный пользователем контур.
- Автотесты не должны создавать реальные заявки.
- Сетевые sandbox-тесты — под маркером `test_sandbox`, не запускаются как
  обычные unit-тесты.

**Торговая и финансовая корректность**

- Токены — только из env, никогда в коде/доках/фикстурах/логах/ошибках.
- `Decimal`, `Quotation`, `MoneyValue` + штатные конвертеры.
- Проверять семантику `quantity` (лоты!), размер лота брать из данных
  инструмента.
- tz-aware даты, UTC.
- Не подменять `figi`, `instrument_id`, `instrument_uid`.
- Клиентский `order_id` сохранять до сетевого вызова, не смешивать с биржевым.
- Никакого слепого retry мутаций: сначала запрашиваем состояние.
- Перед заявкой: торговый статус, доступность покупки/продажи, шаг цены,
  размер лота, лимиты позиции, доступные средства.
- Стратегия формирует намерение; проверка риска и отправка — отдельные
  детерминированные этапы.

**Генерируемый код**

- `*_pb2.py`, `*_pb2_grpc.py`, `*.pyi` не редактируются вручную. Изменение
  контракта → правим proto/генератор → `make gen-client`.
- Код внутри `t_tech/invest/grpc/utils/` правится обычным способом.

**Рабочий процесс**

```bash
poetry install -E all
make test-fast
make lint
make check
make docs
nox -f scripts/noxfile.py -- --cov
poetry run pytest tests/path/to/test_file.py   # узкий прогон
```

**Критерии завершения**

- импорты и сигнатуры существуют в текущем коде;
- тесты не обращаются к реальному контуру;
- токены и персональные данные не попали в diff;
- для финансовых значений не добавлен `float`;
- документация различает реальный контур, песочницу и локальные тесты.

---

## 2.8. Workflow из `SKILL.md` (обязателен для задач про торговую программу)

Скилл `t-invest-python-sdk-bot-builder` описывает полный процесс разработки и
ревью торговых программ. Ключевое:

### Режимы (классифицируем до реализации)

| Режим | Что разрешено |
| :-- | :-- |
| `read-only` | только явно перечисленные операции чтения |
| `sandbox` | мутации через `SandboxClient` / `AsyncSandboxClient` |
| `production` | `Client` / `AsyncClient` на реальном endpoint |
| unit-тест | только локальные моки/фикстуры, без торговой сети |
| бэктест | локальные исторические данные, не песочница |

- Сам по себе `Client` не означает мутацию: режим риска определяется **методом**.
- Наличие токена ≠ разрешение на сделку.
- Нельзя подменять production на sandbox и наоборот ради стиля.
- **Нельзя делать вывод о доходности по песочнице.** Для оценки стратегии —
  отдельный бэктест с комиссиями, проскальзыванием, ликвидностью и защитой от
  look-ahead.
- Режим задаётся **типизированной конфигурацией**, отдельно от промта, ответа
  LLM и рыночных данных.
- Для реальной мутации требуются явно заданные: счёт, инструмент, направление,
  количество лотов, тип заявки, риск-лимиты и порядок обработки неопределённого
  сетевого результата. Если чего-то нет — реализуем read-only или формирование
  намерения и перечисляем, чего не хватает.

### Разделение ответственности

| Компонент | Ответственность |
| :-- | :-- |
| `MarketDataProvider` | получение и нормализация данных |
| `Strategy` / LLM | структурированное торговое намерение, без сетевых мутаций |
| `RiskManager` | детерминированное принятие/отклонение |
| `OrderExecutor` | request-модель и выполнение прошедшей проверки операции |
| `StateStore` | клиентские и биржевые id, состояние, результаты сверки |

- Не вызывать `post_order` из стратегии или LLM.
- В конфигурации задаются: разрешённые режимы, счета, инструменты, действия,
  размер одной заявки и позиции, дневной убыток и прочие лимиты.

### Если в программе участвует LLM

- Валидировать ответ по строгой структуре **до** `RiskManager`.
- Не использовать текстовый `confidence` как размер позиции.
- Новости, веб-страницы, сообщения — **недоверенные данные**; они не могут
  менять режим, системные правила и риск-лимиты.
- При ошибке разбора, отсутствующем поле или неизвестном инструменте —
  отказываться от сделки, а не додумывать значение.
- Сохранять без секретов: входные данные, версию промта, структурированный
  ответ, решение `RiskManager`.

### Проверки перед заявкой

- режим, счёт, однозначно найденный инструмент, допустимое направление;
- торговый статус и доступность покупки/продажи;
- размер лота, положительное количество лотов, шаг и допустимость цены;
- доступные средства/позиция;
- лимиты одной заявки, позиции и дневного убытка;
- тип заявки и все обязательные поля текущей request-модели;
- отсутствие токенов и чувствительных данных в коде, логах, фикстурах и
  сообщениях об ошибках.

### Идемпотентность и восстановление

1. До сетевого вызова создать и сохранить уникальный
   `PostOrderRequest.order_id`.
2. Не смешивать его с `PostOrderResponse.order_id`; сохранять и
   `order_request_id`.
3. При запросе состояния передавать соответствующий `OrderIdType`
   (`ORDER_ID_TYPE_REQUEST` для клиентского ключа).
4. После таймаута/разрыва: не создавать новый ключ, не повторять мутацию вслепую;
   сначала сверить состояние исходной заявки, открытые заявки и сделки; повтор —
   только если доказано, что исходная не принята.
5. Обрабатывать частичное исполнение, отмену, повтор события, восстановление
   после перезапуска. Не смешивать отмену заявок и закрытие позиций.
6. При аварийном завершении: остановить новые сигналы, закрыть клиенты и стримы,
   сохранить состояние для сверки.

### Тесты

Unit-тесты без сетевых торговых запросов на:

- стратегию и строгую валидацию намерения;
- граничные значения размера заявки, позиции и дневного убытка;
- преобразование финансовых типов, округление цены, расчёт лотов;
- повторное событие/сигнал;
- таймаут после отправки, запрет слепого retry, сверку заявки;
- частичное исполнение, отмену, восстановление после перезапуска;
- запрет мутации в read-only и невозможность перехода sandbox → production через
  входные данные.

### Ревью: блокирующие проблемы

- токен/секрет в коде, документации, логе, фикстуре или ошибке;
- неявный переход в production или реальная мутация без явного разрешения;
- прямая отправка заявки из LLM/стратегии без детерминированного риска;
- отсутствие лимита размера заявки/позиции или обязательных pre-order проверок;
- `float` в расчётах денег/цены, ручная сборка `units`/`nano`, отсутствие
  нормализации цены;
- путаница лотов и штук, путаница идентификаторов;
- слепой retry мутации, повторное использование или потеря клиентского ключа
  идемпотентности, смешение с биржевым `order_id`;
- неизвестный или неподтверждённый инструмент;
- сетевой вызов реального торгового API из автотеста;
- восстановление отсутствующих торговых параметров по догадке.

Вердикт ревью: `block` / `changes required` / `ready`.

---

## 2.9. `docs/ai/*` — контекст для LLM

| Файл | О чём |
| :-- | :-- |
| `docs/ai/README.md` | точка входа, приоритет источников, канонические файлы |
| `docs/ai/sdk-map.md` | актуальные импорты, клиенты, сервисы, request-модели, финансовые преобразования, id заявок |
| `docs/ai/real-vs-sandbox.md` | отличия режимов и выбор сценария |
| `docs/ai/trading-safety.md` | границы между стратегией, риск-контролем и исполнением |
| `docs/ai/common-pitfalls.md` | типичные ошибки |
| `docs/ai/prompts/build-trading-program.md` | fallback-промт для разработки |
| `docs/ai/prompts/review-trading-program.md` | fallback-промт для ревью |

### Типичные ошибки (сокращённо)

1. **Старый неймспейс** — `tinkoff.invest.*`, `t_tech.invest.services`.
2. **Аргументы вне request-модели** — после миграции на gRPC методы принимают
   `request=SomeRequest(...)`.
3. **Лоты и штуки** — `quantity` обычно в лотах.
4. **Деньги через `float`** — ошибки округления; только `Decimal` + штатные
   конвертеры; округлять до шага цены до создания модели.
5. **Наивные даты** — только tz-aware UTC.
6. **Смешение идентификаторов** — `figi` ≠ `instrument_id` ≠ `instrument_uid`;
   хранить тип вместе со значением.
7. **Смешение sync и async** — синхронный клиент внутри event loop недопустим.
8. **Слепой retry заявки.**
9. **Песочница как бэктест.**
10. **Редактирование сгенерированных файлов.**

---

## 2.10. Где проверять типы

| Что | Файл |
| :-- | :-- |
| агрегированные модели | `t_tech/invest/grpc/schemas.py` |
| заявки | `t_tech/invest/grpc/orders.py` |
| рыночные данные | `t_tech/invest/grpc/marketdata.py` |
| инструменты | `t_tech/invest/grpc/instruments.py` |
| портфель и операции | `t_tech/invest/grpc/operations.py` |
| песочница | `t_tech/invest/grpc/sandbox.py` |
| исходный контракт | `protos/t_tech/invest/grpc/` |

Сгенерированные сигнатуры текущей версии надёжнее любого примера из другой
версии SDK.

## 2.11. Примеры в репозитории

`examples/` содержит: `all_candles.py`, `async_all_candles.py`,
`async_client.py`, `async_get_candles_with_limit.py`,
`async_get_insider_deals.py`, `async_get_last_prices.py`,
`async_get_market_values.py`, `async_get_orders.py`, `async_get_risk_rates.py`,
`async_get_signals.py`, а также подкаталоги `instruments/`, `sandbox/`,
`strategies/`, `users/`. Импорты и сигнатуры в примерах **нужно сверять** с
актуальным gRPC-кодом — часть примеров обновлялась вместе с переездом на
`t_tech`.

---

## 2.12. Чек-лист интеграции для нашего проекта

- [ ] Пакет `t-tech-investments` установлен через GitLab-индекс, версия
      зафиксирована в lock-файле.
- [ ] Токен читается из env, в git не попадает (`.env` в `.gitignore`).
- [ ] Единственная точка импорта SDK — внутренний адаптер, остальной код про
      SDK ничего не знает.
- [ ] Все деньги проходят через `t_tech.invest.utils`.
- [ ] Клиентский ключ идемпотентности генерируется и пишется в `StateStore`
      **до** сетевого вызова.
- [ ] Режим (`read-only` / `sandbox` / `production`) задаётся типизированным
      конфигом и логируется при старте.
- [ ] sandbox-тесты помечены `test_sandbox` и отключены по умолчанию.
- [ ] В CI есть джоба, сверяющая существование используемых импортов и
      request-моделей с установленной версией пакета.

---

## Источники

- [Репозиторий invest-python](https://opensource.tbank.ru/invest/invest-python)
- [README.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/README.md)
- [AGENTS.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/AGENTS.md)
- [BREAKING_CHANGES.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/BREAKING_CHANGES.md)
- [SKILL.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/.agents/skills/t-invest-python-sdk-bot-builder/SKILL.md)
- [docs/ai/README.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/README.md)
- [docs/ai/sdk-map.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/sdk-map.md)
- [docs/ai/trading-safety.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/trading-safety.md)
- [docs/ai/common-pitfalls.md](https://opensource.tbank.ru/invest/invest-python/-/blob/master/docs/ai/common-pitfalls.md)
- [examples/](https://opensource.tbank.ru/invest/invest-python/-/tree/master/examples)
- [Страница SDK для Python в документации API](https://developer.tbank.ru/invest/sdk/python_sdk/faq_python/)
