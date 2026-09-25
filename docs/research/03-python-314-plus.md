# 03. Python 3.14+ — стандарты, фишки, паттерны, инструменты

Актуально на 2026-09-24.

---

## 3.1. Версии и горизонт планирования

| Версия | Статус | Дата |
| :-- | :-- | :-- |
| **3.14.7** | стабильная, текущая | 7-йMaintenance-релиз серии 3.14 |
| **3.15.0rc2** | release candidate, без ABI-изменений до конца серии | 2026-09-01 |
| **3.15.0** | финал | **2026-10-01** |
| 3.16 | `dev` в документации | — |

**Решение для проекта:** целевая версия — **Python 3.14**, в CI — отдельная
(неблокирующая) джоба на 3.15 до её финала, и блокирующая после 2026-10-01.
Ориентируемся вперёд: `requires-python = ">=3.14"`.

---

## 3.2. Python 3.14 — что важно

### PEP 649 + PEP 749: отложенные аннотации

Аннотации функций, классов и модулей больше не вычисляются жадно — они
складываются в специальные `annotate`-функции и вычисляются только при
необходимости. Следствия:

- дешевле импорт и определение типов;
- **не нужны строковые кавычки для forward-ссылок**;
- интроспекция сохраняется — новый модуль **`annotationlib`** даёт
  `get_annotations(..., format=Format.FORWARDREF | STRING | VALUE)`.

```python
from annotationlib import Format, get_annotations


class OrderIntent:
    # forward-ссылка без кавычек — работает
    parent: "OrderIntent | None" = None


get_annotations(OrderIntent, format=Format.VALUE)
```

Для бота: pydantic/msgspec, строящие схемы по аннотациям, получают более
предсказуемое поведение, а холодный старт — дешевле.

### PEP 750: t-строки

`t"..."` — как f-строка, но возвращает **`string.templatelib.Template`**, а не
`str`. Доступ к частям строки *до* их склейки:

```python
from string.templatelib import Interpolation, Template

name = "World"
tpl = t"Hello {name}!"
tpl.strings  # ("Hello ", "!")
tpl.interpolations  # (Interpolation("World", "name", None, ""),)
tpl.values  # ("World",)
list(tpl)  # ["Hello ", Interpolation(...), "!"]
```

`Interpolation` хранит `value`, `expression` (исходный текст выражения),
`conversion` и `format_spec`.

Практическое применение в торговом боте:

1. **Безопасный SQL** — параметры не попадают в текст запроса, их подставляет
   обработчик:

   ```python
   def query(tpl: Template) -> tuple[str, tuple[object, ...]]:
       text, params = [], []
       for item in tpl:
           if isinstance(item, Interpolation):
               text.append("?")
               params.append(item.value)
           else:
               text.append(item)
       return "".join(text), tuple(params)


   sql, args = query(t"SELECT * FROM candles WHERE uid = {uid} AND ts >= {since}")
   ```

2. **Структурированное логирование** — сообщение и поля из одной записи:

   ```python
   class TMessage:
       def __init__(self, tpl: Template) -> None:
           self.tpl = tpl

       @property
       def message(self) -> str: ...

       @property
       def values(self) -> dict[str, object]:
           return {i.expression: i.value for i in self.tpl if isinstance(i, Interpolation)}
   ```

### PEP 734: сабинтерпретаторы в stdlib

Новый модуль `concurrent.interpreters` — несколько изолированных
интерпретаторов в одном процессе (альтернатива «процессы ради обхода GIL»):

```python
from concurrent import interpreters

interp = interpreters.create()
interp.exec("print('hello from another interpreter')")
```

Пока это больше про встраивание и изоляцию, но направление понятно: CPU-parallel
без тяжёлых процессов.

### PEP 758: `except` без скобок

```python
try:
    ...
except ValueError | KeyError as exc:  # было: except (ValueError, KeyError) as exc
    ...
```

Работает и для `except*`. Скобки при группе типов больше не обязательны.

### PEP 765: управление потоком в `finally`

Компилятор выдаёт **`SyntaxWarning`**, если `return`, `break` или `continue`
покидают блок `finally` (такой код «съедает» исключения). Правило для
проекта: **никаких `return` внутри `finally`** — закрытие ресурсов делаем без
выхода из функции.

### PEP 768: безопасный интерфейс внешнего отладчика

Нулевые накладные расходы в обычном режиме; `pdb` умеет подключаться к
запущенному процессу:

```bash
python -m pdb -p 12345      # remote attach
python -m asyncio ps        # список asyncio-задач процесса
python -m asyncio pstree    # дерево корутин
```

Для бота в проде — редкий, но спасательный инструмент: можно посмотреть, что
делает живой процесс, не перезапуская его.

### PEP 784: Zstandard в stdlib

Новый пакет `compression` с модулями `compression.zstd`, `compression.lzma`,
`compression.bz2`, `compression.gzip`, `compression.zlib`.

Применение: **хранение истории свечей и тиков**. Zstd на таймсериях даёт
сильный выигрыш по месту при высокой скорости распаковки — без внешней
зависимости.

### PEP 779: free-threaded билд официально поддерживается

- Реализация PEP 703 завершена; специализирующий адаптивный интерпретатор
  (PEP 659) включён и в free-threaded сборке.
- Штраф на однопоточном коде — примерно **5–10 %**.
- Флаг `-X context_aware_warnings` (по умолчанию включён в FT-сборке) делает
  фильтры предупреждений безопасными для параллелизма; новый флаг
  `thread_inherit_context` — потоки наследуют `Context()` вызывающего.
- `ctypes` и **`asyncio` получили первоклассную поддержку free-threading**:
  несколько event loop'ов в разных потоках масштабируются линейно.

**Вывод для бота:** free-threading пока не нужен (узкое место — сеть и gRPC),
но код не должен содержать допущений о GIL: никаких «глобальный mutable-кэш без
блокировки», состояние — через явные структуры с защитой или через actor-подход.

### Инкрементальный GC

Два поколения вместо трёх, максимальные паузы на больших кучах снижены на
порядок. Плюс для стриминга котировок: меньше непредсказуемых стоп-мираний при
большом числе живых объектов (свечи, стаканы).

### Прочие изменения 3.14

- Синтаксическая подсветка в PyREPL, цвет в CLIs (`unittest`, `argparse`,
  `json`, `calendar`).
- Встроенная реализация HMAC на формально верифицированном коде HACL*.
- `uuid` v6/v7/v8 и ускорение v3–v5 до 40 %.
- `-X importtime=2` показывает уже загруженные модули; `-c` автоматически
  дедентает код аргумента.
- PEP 761: **PGP-подписи релизов отменены**, рекомендуется Sigstore.
- PEP 776: Emscripten — официально поддерживаемая платформа (tier 3).
- Улучшенные сообщения об ошибках.

---

## 3.3. Python 3.15 — что закладываем уже сейчас

| PEP | Что даёт | Как применимо к боту |
| :-- | :-- | :-- |
| **810** | `lazy import` — отложенная загрузка модуля до первого обращения | быстрый старт CLI-утилит (`lazy import pandas`) |
| **814** | `frozendict` — неизменяемый хешируемый словарь в builtins | конфиги, кэш инструментов как ключ словаря |
| **661** | `sentinel` — штатные уникальные маркеры вместо `_MISSING = object()` | «значение не задано» в отличие от `None` |
| **686** | UTF-8 по умолчанию | одинаковое поведение чтения файлов на всех платформах |
| **798** | распаковка `*`/`**` внутри comprehensions | компактная нормализация данных |
| **799** | пакет `profiling` + профилировщик Tachyon (статистический сэмплинг) | профилирование живого бота с малыми накладными |
| **728** | `TypedDict` с типизированными extra-элементами («закрытые» словари) | строгие контракты JSON-конфигов |
| **747** | `TypeForm` — аннотация самих типов | типизация фабрик по аннотациям |
| **800** | Disjoint bases в системе типов | более точные `Protocol`/ABC |
| **803/820/793** | Стабильная ABI для free-threaded сборок | бинарные колёса без GIL |
| **831** | Frame pointers включены по умолчанию | системная observability, профилирование |
| — | JIT заметно ускорен (≈8–9 % на x86-64 Linux, 12–13 % на AArch64 macOS) | бесплатный прирост, если билд с JIT |

Примеры:

```python
# PEP 810 — только на уровне модуля; внутри функций/классов/try — SyntaxError
lazy import pandas as pd  # pandas загрузится при первом обращении к pd

# PEP 814
RULES: frozendict[str, int] = frozendict({"max_orders_per_min": 15})

# PEP 661
from sentinel import sentinel

MISSING = sentinel("MISSING")


def get(key: str, default: object = MISSING) -> object:
    if default is MISSING:
        raise KeyError(key)
    return default


# PEP 798
flattened = [x for row in rows for x in (*row,)]  # распаковка внутри comprehension
```

Глобальное управление lazy-импортами: `-X lazy_imports=all|normal`,
`PYTHON_LAZY_IMPORTS`, `sys.set_lazy_imports()`, `sys.set_lazy_imports_filter()`,
тип прокси — `types.LazyImportType`.

---

## 3.4. Туллинг 2026 года

| Задача | Инструмент |
| :-- | :-- |
| Окружение, зависимости, версии Python | **uv** (`uv init`, `uv add`, `uv sync`, `uv run`, `uv version --bump`, `uv --python 3.15`) |
| Линт + формат | **ruff** (`ruff check` + `ruff format`) вместо black + isort + flake8 |
| Проверка типов | **mypy** (strict, CI) + **pyright** (редактор); на подходе **ty** (Astral) и **pyrefly** (Meta) |
| Тесты | **pytest** (+ `pytest-asyncio`), матрица версий — `uv --python` или `nox` |
| Хуки | **pre-commit** (`astral-sh/ruff-pre-commit`) |
| Метаданные, зависимости, туллинг | единый `pyproject.toml` (PEP 621) |
| Безопасность зависимостей | `pip-audit` / `uv` audit |
| Логи | `structlog` |
| Данные | `polars` либо `pandas` + `numpy` |
| Валидация внешних границ | `pydantic` v2 | 
| Внутренние DTO | `msgspec.Struct`, `NamedTuple`, frozen `dataclass` |
| Профилирование | `profiling` + Tachyon (3.15), `py-spy`, `-X importtime` + `tuna` |

Схема:

```
git clone → uv sync → uv run ruff check → uv run mypy → uv run pytest
```

---

## 3.5. Структура проекта

```
red-bot-investment/
├── pyproject.toml          # PEP 621: метаданные, deps, ruff, mypy, pytest, uv.index
├── uv.lock
├── .python-version          # 3.14
├── .pre-commit-config.yaml
├── AGENTS.md                # правила для агентов (уже есть)
├── README.md
├── src/red_bot/             # src-layout — импорт из корня невозможен
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py            # pydantic-settings, typed config, режимы
│   ├── logging_setup.py     # structlog
│   ├── domain/              # чистые типы: Decimal, Intents, Decisions
│   ├── data/                # MarketDataProvider, кэш инструментов, стримы
│   ├── strategy/            # сигналы → намерения (без сети!)
│   ├── risk/                # RiskManager (детерминированный)
│   ├── execution/           # OrderExecutor, StateStore, идемпотентность
│   ├── broker/              # единственная обёртка над t_tech.invest.grpc
│   ├── backtest/            # движок бэктеста, метрики, CPCV/WFO
│   └── supervisor/          # kill switch, реконсиляция (отдельный процесс)
├── tests/
│   ├── unit/
│   └── sandbox/             # маркер test_sandbox
└── docs/research/
```

`src/`-layout обязателен: он исключает случайный импорт из корня и проблемы с
namespace-пакетами.

---

## 3.6. Типизация: правила проекта

- **PEP 695 generics** — `def f[T](x: T) -> T`, `class Box[T]: ...`. Больше не
  нужен `TypeVar`.
- **`type` alias** вместо `X = ...`:

  ```python
  type InstrumentKey = tuple[str, str]  # (instrument_uid, class_code)
  type Handler[T] = Callable[[T], None]
  ```

  Такой алиас — объект `TypeAliasType`: ленивый, дженерик, его нельзя
  инстанцировать или использовать в `isinstance`.
- **`Self`** (PEP 673) — возврат себя из builder-методов с корректным типом в
  наследниках.
- **`Protocol`** — структурное подтипирование. Идеален для границ: «всё, что
  умеет `quote(...) -> Decimal`». `@runtime_checkable` — только если нужен
  `isinstance`.
- **`TypeIs`** (PEP 742) предпочтительнее `TypeGuard`: сужает и в `True`, и в
  `False`-ветке.
- **DTO:** `NamedTuple` — для «структуры-кортежа», `TypedDict` — для словарей с
  фиксированной схемой, `frozen dataclass`/`msgspec.Struct` — для доменных
  объектов. С 3.15 — TypedDict с типизированными extra-ключами.
- **Runtime-гарантии** дают не аннотации, а `pydantic`/`msgspec`/`beartype` на
  внешних границах (конфиг, ответ LLM, ответ API). Внутри — статический
  анализатор.
- **`reveal_type` / `assert_type`** — в отладке типов; `cast` — редко и только
  когда уверен.

---

## 3.7. Стиль и практики

- Ruff: включаем `E,F,W,I,N,UP,B,C4,SIM,ASYNC,RUF`, форматтер — `ruff format`
  (совместим с Black > 99,9 %).
- Mypy: `strict = true` для `src/`, плагины — `pydantic.mypy`.
- Иммутабельность по умолчанию: frozen dataclass, `frozendict` (3.15), кортежи.
- Явные доменные исключения; `except` — узкий, без «проглатывания».
- Никаких `return` в `finally` (SyntaxWarning с 3.14).
- Структурированные логи: событие + поля, без конкатенации строк, без секретов.
- Конфигурация — типизированная, валидируется на старте, логируется (без
  секретов).
- Асинхронность: один event loop на поток; синхронный клиент SDK внутри loop
  запрещён. Тяжёлые CPU-куски — в отдельный поток/процесс, не в loop.
- Тесты: без сети по умолчанию, маркеры, property-based для «скучной» арифметики
  денег и округлений.
- Профилирование до оптимизации: `-X importtime`, `python -m asyncio pstree`,
  Tachyon/py-spy.

---

## 3.8. Что берём прямо сейчас в Red-Bot-Investment

| Фича | Берём? | Зачем |
| :-- | :-: | :-- |
| `Decimal` + tz-aware UTC | ✔ | деньги и свечи |
| `Quotation`/`MoneyValue` converters SDK | ✔ | граница API |
| PEP 695 generics + `type`-алиасы | ✔ | читаемые дженерики |
| `Protocol` на границах компонентов | ✔ | тестируемость без мока SDK |
| `Self`, `TypeIs`, `NamedTuple`, frozen dataclass | ✔ | домен |
| `annotationlib` | ✔ | отложенные схемы, дешёвый старт |
| t-строки (PEP 750) | ✔ | безопасный SQL и structlog-сообщения |
| `compression.zstd` (PEP 784) | ✔ | архив свечей/тиков без внешней зависимости |
| `concurrent.interpreters` (PEP 734) | ⚠️ | только если понадобится CPU-параллелизм |
| free-threading (PEP 779) | ✖ | не нужно: упор в сеть; но писать код без допущений о GIL |
| `frozendict`, `sentinel` (3.15) | ⏳ | после 2026-10-01 |
| `lazy import` (3.15) | ⏳ | для CLI-утилит проекта |
| Tachyon (3.15) | ⏳ | профилирование живого бота |

---

## Источники

- [What's new in Python 3.14](https://docs.python.org/3/whatsnew/3.14.html)
- [What's new in Python 3.15](https://docs.python.org/3.15/whatsnew/3.15.html)
- [Python 3.14.7 release notes](https://www.python.org/downloads/release/python-3147/)
- [Python 3.15.0rc2 release notes](https://www.python.org/downloads/release/python-3150rc2/)
- [PEP 750 — Template Strings](https://peps.python.org/pep-0750/)
- [Modern Python Tooling in 2026 — uv, Ruff, ty](https://blog.rajpoot.dev/posts/python/modern-python-tooling-uv-ruff-2026/)
- [Python Best Practices 2026](https://nerdleveltech.com/python-best-practices-the-2025-guide-for-clean-fast-and-secure-code)
- [Building a Python Library in 2026](https://stephenlf.dev/blog/python-library-in-2026/)
- [Explicit Lazy Imports Are Coming to Python 3.15 (JetBrains)](https://blog.jetbrains.com/pycharm/2026/06/explicit-lazy-imports-are-coming-to-python-315/)
- [Python 2026 Typing — Features & Best Practices](https://digon.io/en/blog/2026_04_07_python_typing)
