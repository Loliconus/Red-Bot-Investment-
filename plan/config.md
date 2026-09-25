# CONFIG — техническая спецификация слоя конфигурации

Это фиксирующий документ для слоя `config/`. Это самый маленький по объёму кода, но критичный по ответственности слой: единственное место, где приложение узнаёт о себе — какой контур запущен (live/sandbox/backtest), где токены, куда писать данные, какие лимиты по умолчанию. Ошибка здесь (например, утёкший токен или случайно включённый обход TLS в проде) обходится дороже, чем баг в любом другом слое.

---

## 1. Назначение и границы

### 1.1. Что config ДЕЛАЕТ
- Определяет **типизированную, валидируемую схему** всех параметров запуска приложения (`Settings`).
- Загружает значения из иерархии источников: явные аргументы → переменные окружения → `.env` файл → защищённое хранилище секретов → значения по умолчанию.
- Хранит **бутстрап-артефакты**: TLS-сертификат Russian Trusted CA, шаблон `.env.example`.
- Валидирует конфигурацию **один раз при старте** процесса и падает немедленно (fail-fast), если конфигурация небезопасна или неполна — а не откладывает ошибку до первого реального обращения к API.
- Хранит **значения по умолчанию для первичного заполнения** (seed) операционных настроек (список инструментов, параметры ТА) — но не владеет ими после первого запуска.

### 1.2. Что config НЕ ДЕЛАЕТ (ключевое разграничение, снимающее путаницу)

Здесь важно зафиксировать различие, которое напрямую следует из требований — "хранить набор инструментов и настройки ТА, чтобы менять их в GUI" — это **не** статический конфиг-файл. Есть два принципиально разных вида настроек, и путать их архитектурно вредно:

| | **Bootstrap config** (`config/`) | **Operational config** (домен + БД) |
|---|---|---|
| Примеры | токен API, execution_mode, лимит RAM DuckDB, путь к сертификату | список инструментов, периоды RSI/EMA по фрейму, % риска на сделку |
| Источник | переменные окружения / `.env` / secrets | DuckDB (`strategy_configs`, версионируется) |
| Кто меняет | разработчик/оператор через `.env`, требует **перезапуска** процесса | пользователь через **Web GUI**, применяется "на лету" |
| Валидация | `pydantic-settings`, один раз при старте | доменные правила в `core/`, применяются при сохранении новой версии |
| Версионирование | нет (одно активное значение) | да, история версий хранится в БД |

`config/` отвечает только за первую колонку. Вторая колонка была подробно описана в документах Core (доменные структуры `StrategyConfig`) и Adapters (таблица `strategy_configs` с версионированием) — config-слой лишь **сеет начальные значения** при самом первом запуске пустой БД, дальше не вмешивается.

---

## 2. Технологический выбор: `pydantic-settings`

Для bootstrap-конфигурации используется `pydantic-settings` поверх Pydantic v2 — единственное место всего проекта, где `pydantic` используется вне `adapters/driving/web/` (это осознанное и единственное исключение из правила "Pydantic только в web-слое", так как здесь Pydantic валидирует не HTTP-контракт, а сам процесс запуска).

### 2.1. Источники и приоритет

Pydantic Settings поддерживает автоматическую подгрузку значений из переменных окружения и `.env`-файлов с валидацией типов из коробки, что снимает необходимость вручную писать `os.getenv` и приводить типы. Порядок источников (от высшего приоритета к низшему) настраивается через `settings_customise_sources` и по умолчанию: явно переданные аргументы → переменные окружения → `.env` файл → директория секретов (`secrets_dir`) → значения по умолчанию в коде.

### 2.2. Вложенные настройки через `env_nested_delimiter`

Для группировки связанных параметров (например, всё, что относится к T-Bank API, к хранилищу, к риск-дефолтам) используются вложенные Pydantic-модели, а связь с плоскими именами переменных окружения обеспечивается через `env_nested_delimiter`, позволяющий записывать вложенные значения как `DATABASE__HOST` вместо плоских префиксов. Это даёт структуру конфигурации, зеркальную структуре `AppContext`, без создания вручную парсера плоских строк.

### 2.3. Секреты — `SecretStr` и осознанный выбор хранилища для локального ПК

Общая рекомендация индустрии: секреты никогда не должны быть захардкожены в коде — их следует хранить вне кодовой базы и вне системы контроля версий, а один из базовых способов для локальной разработки — файл `.env`, обязательно исключённый из git. При этом стандартная встроенная реализация `SecretsSettingsSource` в Pydantic имеет ограничение — она не поддерживает секреты во вложенных подмоделях без дополнительной настройки, что нужно учитывать при проектировании вложенной схемы токенов.

Для проекта, работающего на **личном ПК одного пользователя** (не Docker/Kubernetes-окружение с `/run/secrets`), разумный баланс — два уровня:

1. **`.env` для разработки** — обязательно в `.gitignore`, никогда не коммитится; локальный кеш переменных для быстрого старта.
2. **`keyring` (доступ к системному хранилищу учётных данных ОС) — рекомендуемый способ для боевого токена**: вместо хранения в открытом текстовом файле токен один раз кладётся в системное хранилище паролей ОС (Windows Credential Manager/macOS Keychain/Linux Secret Service) через библиотеку `keyring`, которая предоставляет единый интерфейс для безопасного извлечения секретов без их хранения в коде.

```python
# config/secrets_source.py
import keyring
from pydantic_settings import PydanticBaseSettingsSource

class KeyringSettingsSource(PydanticBaseSettingsSource):
    """Кастомный источник: читает токен из системного хранилища ОС, если он там есть."""
    def get_field_value(self, field, field_name):
        value = keyring.get_password("red-bot", field_name)
        return value, field_name, False

    def __call__(self) -> dict:
        token = keyring.get_password("red-bot", "tbank_api_token")
        return {"tbank": {"api_token": token}} if token else {}
```

Приоритет источников для `Settings.settings_customise_sources`: явные аргументы → переменные окружения → `.env` → **KeyringSettingsSource** → значения по умолчанию. Таким образом `.env` остаётся для некритичных параметров разработки, а боевой токен вообще не существует ни в одном текстовом файле проекта.

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
    account_id: str                                  # managed_account_id — единственный разрешённый счёт
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
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",             # незнакомая переменная — ошибка, а не молчаливый игнор
        case_sensitive=False,
    )

    execution_mode: ExecutionMode = ExecutionMode.SANDBOX
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

    @model_validator(mode="after")
    def _forbid_default_account_in_live(self) -> "Settings":
        if self.execution_mode is ExecutionMode.LIVE and not self.tbank.account_id:
            raise ValueError("account_id обязателен для LIVE режима")
        return self
```

### 4.1. Соответствующий `.env.example` (коммитится в репозиторий как шаблон)

```dotenv
REDBOT_EXECUTION_MODE=sandbox

REDBOT_TBANK__API_TOKEN=changeme
REDBOT_TBANK__ACCOUNT_ID=changeme
REDBOT_TBANK__INSECURE_TLS_DEV_ONLY=false

REDBOT_STORAGE__DUCKDB_MEMORY_LIMIT_MB=1536
REDBOT_STORAGE__DUCKDB_THREADS=2

REDBOT_WEB__SESSION_SECRET=changeme
```

Реальный `.env` создаётся локально и никогда не попадает в git — как отмечается в общей практике, файл `.env` должен оставаться локальным и не подлежит версионированию, поскольку в противном случае секреты остаются читаемыми в истории репозитория даже после последующего удаления файла.

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
| Пустой `account_id` в live | `Settings._forbid_default_account_in_live` | Процесс не стартует |
| Неизвестная переменная окружения | `extra="forbid"` в `SettingsConfigDict` | Процесс не стартует — защита от опечаток в `.env` |
| Секрет в логах | `redact_processor` в `logging_config.py` | Значение маскируется на уровне логгера |
| Токен в файле репозитория | `.gitignore` + рекомендация pre-commit сканера секретов | Организационная защита, не код |

---

## 11. Тестирование config-слоя

`tests/unit/config/` проверяет исключительно валидаторы: подать заведомо невалидную комбинацию (`execution_mode=live` + `insecure_tls_dev_only=true`) и убедиться, что `Settings()` кидает исключение при инициализации, до того как процесс успеет сделать хоть один сетевой вызов. Реальный `.env`/keyring в юнит-тестах не используется — тестовые значения передаются через `Settings(**explicit_kwargs)`, что имеет наивысший приоритет среди источников.

---

## Резюме: почему это решает исходные задачи

- **SSL-проблема решена на уровне конфигурации, а не кода** — путь к сертификату и аварийный флаг обхода — явные, валидируемые поля, а не magic-константы, разбросанные по проекту.
- **Токен никогда не хранится в открытом виде на диске в боевом режиме** — используется системное хранилище ОС через `keyring`, `.env` остаётся только для локальной разработки в sandbox.
- **Разграничение bootstrap/operational конфигурации** — устраняет главную потенциальную путаницу: "настройки инструментов и ТА" из требований пользователя не размазаны по `.env`-файлам, а живут там, где им место — в БД с версионированием, доступные из GUI, тогда как `config/` отвечает только за то, что нужно для самого факта запуска процесса.
- **Fail-fast валидация** — невозможно случайно запустить бота в LIVE-режиме с обходом проверки сертификата или без указания торгового счёта — ошибка конфигурации останавливает процесс до того, как он успеет что-то сделать.