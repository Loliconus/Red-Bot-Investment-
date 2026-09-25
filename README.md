# Red-Bot-Investment

Торговый робот для **T‑Invest API** (Т‑Инвестиции) на **Python 3.14+**.

Профиль: **свинг + интрадей, только LONG**, 2–5 бумаг из MOEX с обязательным
сравнением силы against бенчмарка IMOEX (сам IMOEX не торгуется).

> Статус: исследование завершено, **этапы 1–5 разработки выполнены** (ядро,
> приложение, адаптеры, GUI, тесты). Боевой запуск (`LIVE`) заблокирован
> до прохождения чек-листа — см. <!--citation:12-->.

---

## Быстрый старт (рекомендуется: uv)

Проект использует **uv**:
- создаёт `.venv`,
- фиксирует зависимости в `uv.lock`,
- поднимает одинаковое окружение на всех машинах.

```bash
# 0) один раз: поставить uv (если не установлен)
# см. официальную установку uv для вашей ОС

# 1) Python: закрепить версию (создаст .python-version)
uv python install 3.14
uv python pin 3.14

# 2) зависимости + виртуалка + lockfile
uv sync

# 3) токен — в системное хранилище (не в .env и не в git)
uv run redbot secrets set-token

# 4) первичное заполнение БД и запуск
uv run redbot db bootstrap
uv run redbot run --mode sandbox
uv run redbot run --mode backtest --no-gui
Web GUI поднимается вместе с торговым циклом: http://<хост>:8080/docs.

Быстрый старт (fallback: pip/venv)
Важно: зависимость t-tech-investments ставится из индекса T‑Bank, поэтому для pip нужно добавить extra index.

Bash

python -m venv .venv && source .venv/bin/activate

# чтобы pip видел t-tech-investments
export PIP_EXTRA_INDEX_URL="https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"

pip install -e ".[dev]"

# токен
redbot secrets set-token  # или: python main.py secrets set-token

# запуск
python main.py db bootstrap
python main.py run --mode sandbox
Важно про T‑Invest API (коротко, но критично)
Контуры и адреса
Prod: invest-public-api.tbank.ru:443
Sandbox: sandbox-invest-public-api.tbank.ru:443
Код должен уметь работать в sandbox “тем же способом”, что и в prod (в основном отличаются адресом и некоторыми возможностями/методами).
Не используйте другие адреса подключения.

Авторизация
Токен передаётся в metadata как Authorization: Bearer <token>. Есть разные типы токенов (read-only, full-access, sandbox и т.д.). Sandbox‑токен нельзя использовать против prod‑методов.

Идемпотентность заявок
Для postOrder используйте order_id как ключ идемпотентности (генерируйте до сетевого вызова, UUID‑формат). Для PostOrderAsync order_id обязателен (UUID любой версии).

В стриме заявок order_request_id корректно коррелируется с вашим ключом только если вы передаёте order_id в UID/UUID формате — иначе API может подменить его.

Сеть/SSL
Если ловите ошибки TLS вида certificate verify failed — проверьте установку корневых сертификатов, которые требуются для доступа к API.

Переменные окружения
Префикс REDBOT__, вложенность через __:

Bash

REDBOT__EXECUTION_MODE=sandbox
REDBOT__TBANK__API_TOKEN=...        # либо keyring (ниже по приоритету)
REDBOT__TBANK__ACCOUNT_ID=...
REDBOT__STORAGE__DATA_DIR=./data
REDBOT__LOG_LEVEL=INFO
Неизвестные ключи — ошибка (extra=forbid): опечатка в .env не пройдёт молча.
Шаблон: 
13
.

Архитектура
Гексагональная (ports & adapters), ровно 6 портов как typing.Protocol: MarketData, OrderExecution, Repository, Archive, Clock, Notification. Ядро (core/) не содержит внешних зависимостей; направление зависимостей: config → application → core, adapters → core.

text

adapters/driving  (FastAPI+WS GUI, CLI)
        ↓
application       (юзкейсы, composition, event bus, kill switch)
        ↓
core              (domain, analysis, risk, strategy, journal)
        ↑
adapters/driven   (T‑Invest, sandbox, backtest, storage DuckDB+Parquet)
Подробности — 
14
.

Ключевые правила проекта
Правило	Почему
Деньги и цены — только Decimal	float копит ошибку округления на деньгах
Ключ идемпотентности формируется до сетевого вызова	иначе повтор после таймаута удваивает сделку
Мутации без ключа идемпотентности не повторяются	то же самое
Hard stop ставится всегда	это защита капитала, а не «настройка»
Цель ≥ издержки × 2	сделка обязана окупать вход-выход с запасом
Торговля только с managed_account_id	чужие счета блокируются адаптером
Гипотеза применяется только вручную	автоприменение = подгонка по истории
LIVE только после sandbox + walk-forward + явного решения	см. чек-лист
Документация
Спецификация
Документ	О чём
15
Рабочая фиксация ТЗ v1.0
14
Слои, порты, реализации
12
Этапы 1–8, чек-лист выхода в бой
Исследования
Документ	О чём
16
Сводный конспект
17
Контуры, сервисы, лимиты, ошибки
18
Python SDK t-tech-investments
19
Python 3.14+ и tooling
20
Бэктест и риск-инфраструктура
21
Синтез: целевая архитектура
Правила работы с репозиторием для агентов — 
22
.

Разработка
Bash

ruff check . && ruff format .     # линтер и форматтер
mypy                              # strict, только исходники
pytest -m "not sandbox"           # юнит + контрактные + интеграционные
pytest -m sandbox                 # требует токен песочницы
Структура тестов:

tests/unit/ — ядро и юзкейсы на фейках, без сети и внешних библиотек;
tests/contract/ — один набор проверок на все реализации порта;
tests/integration/ — composition root, Web GUI, сквозной бэктест-прогон.
Что не входит в MVP
Шорты и маржинальные плечи, HFT и скальпинг, волны Эллиотта и паттерны XABCD, авто-применение гипотез, тиковая запись стакана, поддержка нескольких брокеров.

Дисклеймер
Проект не является инвестиционной рекомендацией. Торговля на финансовых рынках связана с риском потери капитала.