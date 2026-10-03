# CONFIG — техническая спецификация слоя конфигурации

Это фиксирующий документ для слоя `config/`. Он описывает типизированные bootstrap-настройки (токен, транспорт и ресурсы). Режим и счёт следующего запуска — пользовательские настройки: приложение хранит их в DuckDB `operational_settings`, даёт менять через GUI/CLI и не требует ручного `.env`. `Settings.execution_mode` остаётся fallback при первом запуске; account ID в Settings/ENV — необязательный явный override, который проверяется через API.

---

## 1. Назначение и границы

### 1.1. Что config ДЕЛАЕТ
- Определяет **типизированную, валидируемую схему** всех параметров запуска приложения (`Settings`).
- Валидирует секреты, транспорт и системные лимиты; обычный локальный запуск не требует заполнения `.env`.
- Поддерживает переменные окружения/`.env` как необязательные overrides для секретов и развёртывания.
- Хранит TLS-артефакты и шаблон необязательных overrides.
- При первом запуске использует `Settings.execution_mode` как fallback; после сохранения режим по умолчанию берётся из DuckDB. Account ID можно не задавать: адаптер получает список открытых счетов и выбирает по правилам приложения.

### 1.2. Что config НЕ ДЕЛАЕТ (ключевое разграничение, снимающее путаницу)

Здесь важно зафиксировать различие, которое напрямую следует из требований — "хранить набор инструментов и настройки ТА, чтобы менять их в GUI" — это **не** статический конфиг-файл. Есть два принципиально разных вида настроек, и путать их архитектурно вредно:

| | **Bootstrap secrets/resources** (`config/`) | **Операционные и пользовательские настройки** (DuckDB) |
|---|---|---|
| Примеры | токен API, TLS, лимиты ресурсов, advanced overrides | режим следующего запуска, счёт по режиму, список инструментов, риск-настройки |
| Источник | keyring для токена; ENV/`.env` необязательны | `operational_settings` или `strategy_configs` |
| Кто меняет | оператор/развёртывание; секреты не показываются GUI | пользователь через Web GUI или CLI; execution mode применяется после рестарта |
| Валидация | `pydantic-settings`; API-секрет/настройки сверяются при подключении | правила use case; account ID обязательно сверяется со списком открытых счетов текущего контура |
| Версионирование | нет | стратегия версионируется; режим и account defaults — отдельными ключами |

`config/` отвечает за bootstrap и безопасную загрузку секретов. Режим и account ID — не секреты и не требуют ручного `.env`: ключ `execution_mode` хранит default-контур; `managed_account_id:live` и `managed_account_id:sandbox` хранят независимые предпочтения. Общий старый `managed_account_id` допускается только как миграционный fallback после проверки счёта через соответствующий API.

---

## 2. Технологический выбор: `pydantic-settings`

Для bootstrap-конфигурации используется `pydantic-settings` поверх Pydantic v2 — единственное место всего проекта, где `pydantic` используется вне `adapters/driving/web/` (это осознанное и единственное исключение из правила "Pydantic только в web-слое", так как здесь Pydantic валидирует не HTTP-контракт, а сам процесс запуска).

### 2.1. Источники и приоритет

Pydantic Settings поддерживает автоматическую подгрузку environment overrides и их типизацию. В этом приложении это вспомогательный путь для токена и настроек развёртывания, не обязательный пользовательский конфиг. Порядок `settings_customise_sources`: явные аргументы → переменные окружения → `.env` → file secrets → `KeyringSettingsSource` → defaults. После создания Settings сохранённый режим запуска из DuckDB имеет приоритет над `Settings.execution_mode`; CLI `--mode` — явный override.

### 2.2. Вложенные настройки через `env_nested_delimiter`

Для группировки связанных параметров (например, всё, что относится к T-Bank API, к хранилищу, к риск-дефолтам) используются вложенные Pydantic-модели, а связь с плоскими именами переменных окружения обеспечивается через `env_nested_delimiter`, позволяющий записывать вложенные значения как `DATABASE__HOST` вместо плоских префиксов. Это даёт структуру конфигурации, зеркальную структуре `AppContext`, без создания вручную парсера плоских строк.

### 2.3. Секреты — `SecretStr` и системное хранилище

Токены и пароли не хранятся в DuckDB, коде, тестах или логах. Токен T-Invest по умолчанию сохраняется в системный keyring через `redbot secrets set-token`; для контейнерных развёртываний разрешено передать секрет через environment/secret manager. `.env` не требуется для обычного запуска, а реальные секреты никогда не должны включаться в `.env.example` или Git.

`KeyringSettingsSource` заполняет поле API-токена вложенной модели Settings. Поля типа `SecretStr` скрывают значение в repr; доступ к нему требует явного вызова `.get_secret_value()`. GUI поддерживает write-only изменение токена и не возвращает значение клиенту.

ENV/`.env` могут быть включены как advanced overrides для автоматизированной среды, но не являются местом хранения пользовательского режима или account ID. Последние сохраняются в DuckDB отдельными ключами; токен из них исключён.

### 2.4. Поля типа `SecretStr` — маскирование в логах и repr

Любое поле, содержащее токен/пароль, объявляется как `SecretStr`, а не `str`. Значение при этом не отображается в текстовом представлении объекта и логах по умолчанию, доступ к реальному значению требует явного вызова `.get_secret_value()` — это защита от случайного попадания токена в лог при отладочном `print(settings)` или необработанном исключении.

---

## 3. Структура `config/`

```
config/
├── settings.py            # Settings — единственная точка правды bootstrap-конфига
├── enums.py                # ExecutionMode и др. bootstrap-специфичные enum
├── secrets_source.py        # KeyringSettingsSource — кастомный источник секретов
├── logging_config.py        # настройка structlog/logging, редактирование секретов из логов
├── seed_defaults.py         # значения для первичного заполнения operational config в БД
├── certs/
│   └── russian_trusted_ca.pem
├── .env.example              # шаблон, коммитится; .env — НЕ коммитится
└── .gitignore                # локально: .env, *.pem с приватными ключами (если появятся)
```

---

## 4. `settings.py` — полная зафиксированная схема

```python
# config/settings.py
from decimal import Decimal
from pathlib import Path
from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from config.enums import ExecutionMode


class TBankSettings(BaseModel):
    api_token: SecretStr
    account_id: str = ""                             # необязательный явный override; иначе авто-выбор по API
    grpc_target_live: str = "invest-public-api.tbank.ru:443"
    grpc_target_sandbox: str = "sandbox-invest-public-api.tbank.ru:443"
    ca_bundle_path: Path = Path("config/certs/russian_trusted_ca.pem")
    insecure_tls_dev_only: bool = False               # аварийный люк, блокируется в LIVE
    max_subscriptions_per_channel: int = 300           # явный контроль лимита стрима


class StorageSettings(BaseModel):
    data_dir: Path = Path("data")
    duckdb_memory_limit_mb: int = 1536                # жёсткий потолок RAM — не мешать дневной работе ПК
    duckdb_threads: int = 2
    disk_usage_threshold_pct: float = 0.8              # порог запуска архивации
    hot_retention_days: int = 1
    warm_retention_days: int = 180


class RiskDefaultsSettings(BaseModel):
    commission_rate: Decimal = Decimal("0.003")        # 0.3% за сделку
    min_viable_target_multiplier: Decimal = Decimal("2.0")
    max_risk_per_trade_pct: Decimal = Decimal("0.01")  # 1% депозита на сделку по умолчанию
    hypothesis_min_sample_size: int = 30
    walk_forward_confirmation_threshold: Decimal = Decimal("0.5")


class WebSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    session_secret: SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REDBOT_",
        env_nested_delimiter="__",
        env_file=".env",          # необязательный advanced override, не нужен для обычного запуска
        env_file_encoding="utf-8",
        extra="forbid",             # незнакомая переменная — ошибка, а не молчаливый игнор
        case_sensitive=False,
    )

    execution_mode: ExecutionMode = ExecutionMode.SANDBOX  # fallback только при отсутствии DuckDB default
    tbank: TBankSettings
    storage: StorageSettings = StorageSettings()
    risk_defaults: RiskDefaultsSettings = RiskDefaultsSettings()
    web: WebSettings

    @model_validator(mode="after")
    def _forbid_insecure_tls_in_live(self) -> "Settings":
        if self.execution_mode is ExecutionMode.LIVE and self.tbank.insecure_tls_dev_only:
            raise ValueError(
                "insecure_tls_dev_only запрещён в режиме LIVE — уберите флаг или смените режим"
            )
        return self

    # account_id may be blank: the startup resolver lists only OPEN accounts,
    # validates explicit overrides, and requires CLI selection for tied choices.
```

### 4.1. Соответствующий `.env.example` (коммитится в репозиторий как шаблон)

```dotenv
# Все строки ниже — необязательные advanced overrides.
# Не задавайте execution mode/account для обычного использования: их хранит DuckDB.
REDBOT_STORAGE__DUCKDB_MEMORY_LIMIT_MB=1536
REDBOT_STORAGE__DUCKDB_THREADS=2
REDBOT_WEB__PORT=8000
REDBOT_LOG_LEVEL=info
# Секреты могут передаваться из secret manager/ENV; не коммитьте реальные значения.
```

Пример не требует копирования в `.env`. Режим сохраняется в `operational_settings.execution_mode`; предпочтения счетов разделены по `live` и `sandbox`. `--mode`/`--account` — CLI overrides, `/settings` — GUI для режима следующего старта и выбора открытого счёта активного контура. При пустом списке sandbox приложение открывает один счёт без pay-in; если несколько равно приоритетных, CLI просит оператора выбрать. API-ошибки не переключают приложение на симулятор.

---

## 5. Enum bootstrap-уровня

```python
# config/enums.py
from enum import StrEnum

class ExecutionMode(StrEnum):
    LIVE = "live"
    SANDBOX = "sandbox"
    BACKTEST = "backtest"
```

Обратите внимание: это единственный enum, который дублируется по смыслу между `config/` и доменом — намеренно. `core/` не должен зависеть от `config/enums.py` (это нарушило бы направление зависимостей ядра), поэтому `ExecutionMode` существует только здесь и используется исключительно в `composition.py` для выбора адаптеров — сам домен вообще не знает о понятии "режим исполнения", для него любой режим — это просто конкретная реализация портов.

---

## 6. `seed_defaults.py` — начальное состояние операционного конфига

Как зафиксировано в разделе 1.2, это единственная связь между bootstrap-конфигом и доменными операционными настройками — используется **только** при первом запуске на пустой БД:

```python
# config/seed_defaults.py
DEFAULT_INSTRUMENTS = [
    {"ticker": "SBER", "is_benchmark": False},
    {"ticker": "IMOEX", "is_benchmark": True},
]

DEFAULT_TA_PARAMS: dict[Timeframe, dict[str, dict]] = {
    Timeframe.D1: {"rsi": {"period": 14}, "ema": {"periods": [50, 200]}},
    Timeframe.H1: {"rsi": {"period": 9}, "macd": {"fast": 12, "slow": 26, "signal": 9}},
    Timeframe.M1: {"vwap": {}, "atr": {"period": 14}},
}
```

Юзкейс `application/use_cases/bootstrap_database.py` (не относится к `config/`, но потребляет его данные) при пустой таблице `strategy_configs` создаёт первую версию (`version=1`) из этих значений и дальше никогда не обращается назад к `config/` — вся дальнейшая жизнь этих параметров происходит в БД через GUI.

---

## 7. Логирование и защита секретов в логах

`logging_config.py` настраивает структурированное логирование (`structlog` поверх стандартного `logging`) с обязательным процессором маскирования — любое поле с именем, содержащим `token`/`password`/`secret`/`api_key`, автоматически заменяется на `***` перед записью, независимо от того, откуда пришла строка лога (это защита на случай, если разработчик по ошибке залогирует весь объект настроек, а не отдельное безопасное поле).

```python
# config/logging_config.py
SENSITIVE_KEYS = {"api_token", "session_secret", "password"}

def redact_processor(logger, method_name, event_dict):
    for key in event_dict:
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict
```

---

## 8. Загрузка и жизненный цикл `Settings`

### 8.1. Единая точка загрузки

```python
# config/settings.py (продолжение)
_settings_instance: Settings | None = None

def load_settings() -> Settings:
    """Вызывается РОВНО ОДИН РАЗ за жизнь процесса — в точке входа."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()  # здесь падает fail-fast при невалидной конфигурации
    return _settings_instance
```

`load_settings()` вызывается только в `main.py`/`adapters/driving/web/app.py` (lifespan) — нигде глубже в `application/`/`core/` эта функция не импортируется повторно; `Settings`-объект передаётся вниз явно через `AppContext.settings`, как зафиксировано в документе Application.

### 8.2. Неизменяемость в течение жизни процесса

`Settings` не имеет метода "перезагрузить из окружения на лету" — bootstrap-конфигурация фиксирована на весь запуск процесса. Если пользователь меняет, например, `duckdb_memory_limit_mb` в GUI-разделе администрирования БД (описанном в документе Adapters), это **не** переопределяет `Settings` в памяти — вместо этого GUI обновляет значение в отдельной таблице operational-настроек в БД, а изменение реально применяется только при следующем перезапуске процесса **или** через явный adapter-level метод (`conn.execute("SET memory_limit=...")`), если параметр допускает горячее применение без рестарта. Это разграничение (какие параметры требуют рестарта, а какие горячо применяются) явно документируется в самом GUI рядом с полем настройки.

---

## 9. Соглашения об именовании

| Категория | Правило | Пример |
|---|---|---|
| Переменные окружения | `REDBOT_` + `UPPER_SNAKE_CASE`, `__` для вложенности | `REDBOT_TBANK__API_TOKEN` |
| Pydantic-модели настроек | суффикс `Settings` | `TBankSettings`, `StorageSettings` |
| Секретные поля | всегда тип `SecretStr`, никогда `str` | `api_token: SecretStr` |
| Bootstrap enum | суффикс отсутствует, PascalCase класс, snake_case значения | `ExecutionMode.SANDBOX` |
| Файлы сертификатов | описательное имя + расширение `.pem` | `russian_trusted_ca.pem` |
| Seed-данные | префикс `DEFAULT_`, UPPER_SNAKE_CASE константа | `DEFAULT_INSTRUMENTS` |
| Функции загрузки конфигурации | глагол + `settings`, snake_case | `load_settings()` |

---

## 10. Валидация и защитные механизмы (сводка)

| Проверка | Где | Последствие при нарушении |
|---|---|---|
| `insecure_tls_dev_only=True` в `execution_mode=live` | `Settings._forbid_insecure_tls_in_live` | Процесс не стартует, `ValueError` при инициализации |
| Явный account ID отсутствует | startup account resolver | API возвращает открытые счета; однозначный выбирается автоматически, tie требует выбор CLI; в LIVE без открытых счетов запуск завершается ошибкой |
| Неизвестная переменная окружения | `extra="forbid"` в `SettingsConfigDict` | Процесс не стартует — защита от опечаток в `.env` |
| Секрет в логах | `redact_processor` в `logging_config.py` | Значение маскируется на уровне логгера |
| Токен в файле репозитория | `.gitignore` + рекомендация pre-commit сканера секретов | Организационная защита, не код |

---

## 11. Тестирование config-слоя

`tests/unit/config/` проверяет исключительно валидаторы: подать заведомо невалидную комбинацию (`execution_mode=live` + `insecure_tls_dev_only=true`) и убедиться, что `Settings()` кидает исключение при инициализации, до того как процесс успеет сделать хоть один сетевой вызов. Реальный `.env`/keyring в юнит-тестах не используется — тестовые значения передаются через `Settings(**explicit_kwargs)`, что имеет наивысший приоритет среди источников.

---

## Резюме: почему это решает исходные задачи

- **SSL-проблема решена на уровне конфигурации, а не кода** — путь к сертификату и аварийный флаг обхода — явные, валидируемые поля, а не magic-константы, разбросанные по проекту.
- **Секреты изолированы** — API-токен хранится в системном keyring либо в secret manager/ENV и никогда не помещается в DuckDB, код, тесты или логи.
- **Режим и счета не требуют `.env`** — режим следующего запуска и раздельные account defaults хранятся в DuckDB, изменяются из GUI/CLI и применяются при рестарте.
- **Счёт валидируется у брокера** — явный ID должен быть OPEN; иначе приложение выбирает счет по приоритету или просит CLI-оператора выбрать. В LIVE отсутствие счетов — ошибка, sandbox может открыть один новый счёт без автоматического пополнения.
- **TLS fail-fast** — обход проверки сертификата не поддерживается; ошибки реального API не маскируются переходом в симулятор.