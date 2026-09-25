# Red-Bot-Investment

Торговый робот для **T-Invest API** (Т-Инвестиции) на **Python 3.14+**.

Профиль: **свинг + интрадей, только LONG**, 2–5 бумаг изMOEX с обязательным
сравнением силы against бенчмарка IMOEX (сам IMOEX не торгуется).

> Статус: исследование завершено, **этапы 1–5 разработки выполнены** (ядро,
> приложение, адаптеры, GUI, тесты). Боевой запуск (`LIVE`) заблокирован
> до прохождения чек-листа — см. [`docs/spec/roadmap.md`](docs/spec/roadmap.md).

## Быстрый старт

```bash
# окружение
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# SDK Т-Инвестиций — из GitLab-индекса Т-Банка (не PyPI)
pip install t-tech-investments \
  --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple

# токен — в системное хранилище (не в .env и не в git)
redbot secrets set-token         # или: python main.py secrets set-token

# первичное заполнение БД и запуск
python main.py db bootstrap
python main.py run --mode sandbox
python main.py run --mode backtest --no-gui
```

Web GUI поднимается вместе с торговым циклом: `http://<хост>:8080/docs`.

### Переменные окружения

Префикс `REDBOT__`, вложенность через `__`:

```bash
REDBOT__EXECUTION_MODE=sandbox
REDBOT__TBANK__API_TOKEN=...        # либо keyring (ниже по приоритету)
REDBOT__TBANK__ACCOUNT_ID=...
REDBOT__STORAGE__DATA_DIR=./data
REDBOT__LOG_LEVEL=INFO
```

Неизвестные ключи — ошибка (`extra=forbid`): опечатка в `.env` не пройдёт
молча. Шаблон: [`config/.env.example`](config/.env.example).

## Архитектура

Гексагональная (ports & adapters), ровно **6 портов** как `typing.Protocol`:
`MarketData`, `OrderExecution`, `Repository`, `Archive`, `Clock`,
`Notification`. Ядро (`core/`) не содержит внешних зависимостей; направление
зависимостей: `config → application → core`, `adapters → core`.

```
adapters/driving  (FastAPI+WS GUI, CLI)
        ↓
application       (юзкейсы, composition, event bus, kill switch)
        ↓
core              (domain, analysis, risk, strategy, journal)
        ↑
adapters/driven   (T-Invest, sandbox, backtest, storage DuckDB+Parquet)
```

Подробности — [`docs/spec/architecture.md`](docs/spec/architecture.md).

## Ключевые правила проекта

| Правило | Почему |
| :-- | :-- |
| Деньги и цены — только `Decimal` | `float` копит ошибку округления на деньгах |
| Ключ идемпотентности формируется **до** сетевого вызова | иначе повтор после таймаута удваивает сделку |
| Мутации без ключа идемпотентности **не повторяются** | то же самое |
| Hard stop ставится всегда | это защита капитала, а не «настройка» |
| Цель ≥ издержки × 2 | сделка обязана окупать вход-выход с запасом |
| Торговля только с `managed_account_id` | чужие счета блокируются адаптером |
| Гипотеза применяется только вручную | автоприменение = подгонка по истории |
| `LIVE` только после sandbox + walk-forward + явного решения | см. чек-лист |

## Документация

### Спецификация

| Документ | О чём |
| :-- | :-- |
| [`docs/spec/ТЗ.md`](docs/spec/ТЗ.md) | Рабочая фиксация ТЗ v1.0: обязательные решения по риску, архитектуре, домену, хранению |
| [`docs/spec/architecture.md`](docs/spec/architecture.md) | Как устроен код: слои, порты, реализации, решения ядра |
| [`docs/spec/roadmap.md`](docs/spec/roadmap.md) | Этапы 1–8, что сделано, чек-лист выхода в бой |

### Исследования

| Документ | О чём |
| :-- | :-- |
| [`docs/research/00-index.md`](docs/research/00-index.md) | Сводный конспект: 25 ключевых выводов по всем четырём темам |
| [`docs/research/01-tinvest-api.md`](docs/research/01-tinvest-api.md) | T-Invest API: контуры, сервисы, идентификаторы, заявки, стримы, лимиты, ошибки |
| [`docs/research/02-tinvest-python-sdk.md`](docs/research/02-tinvest-python-sdk.md) | Python SDK `t-tech-investments`: неймспейс `t_tech.invest.grpc`, каноны из `AGENTS.md`/`SKILL.md` |
| [`docs/research/03-python-314-plus.md`](docs/research/03-python-314-plus.md) | Python 3.14 / 3.15: PEP, что брать в продакшн, туллинг, типизация |
| [`docs/research/04-trading-guides.md`](docs/research/04-trading-guides.md) | Бэктест и его ловушки, метрики, сайзинг, риск-инфраструктура, MOEX и налоги РФ |
| [`docs/research/05-red-bot-blueprint.md`](docs/research/05-red-bot-blueprint.md) | Синтез: целевая архитектура и дорожная карта проекта |

Правила работы с репозиторием для агентов — [`AGENTS.md`](AGENTS.md).

## Разработка

```bash
ruff check . && ruff format .     # линтер и форматтер
mypy                              # strict, только исходники
pytest -m "not sandbox"           # юнит + контрактные + интеграционные
pytest -m sandbox                 # требует токен песочницы
```

Структура тестов:

* `tests/unit/` — ядро и юзкейсы на фейках, без сети и внешних библиотек;
* `tests/contract/` — один набор проверок на все реализации порта
  (фейк ↔ адаптер);
* `tests/integration/` — composition root, Web GUI, сквозной бэктест-прогон.

## Что не входит в MVP

Шорты и маржинальные плечи, HFT и скальпинг, волны Эллиотта и паттерны XABCD,
авто-применение гипотез, тиковая запись стакана, поддержка нескольких брокеров.

## Дисклеймер

Проект не является инвестиционной рекомендацией. Торговля на финансовых
рынках связана с риском потери капитала.
