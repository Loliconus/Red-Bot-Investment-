"""Единственная точка правды bootstrap-конфигурации.

Принципы:
* fail-fast — процесс падает на инициализации ``Settings``, до первого сетевого
  вызова, если конфигурация неполна или небезопасна;
* ``extra="forbid"`` — опечатка в ``.env`` это ошибка, а не молчаливый игнор;
* секреты — только ``SecretStr``;
* ``Settings`` неизменяем в течение жизни процесса и не перечитывается.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.enums import ExecutionMode, LogLevel
from config.secrets_source import KeyringSettingsSource

ENV_PREFIX = "REDBOT_"


class TBankSettings(BaseModel):
    """Параметры подключения к T-Invest API."""

    api_token: SecretStr
    account_id: str = Field(
        default="",
        description="managed_account_id — единственный счёт, с которым разрешена торговля",
    )
    grpc_target_live: str = "invest-public-api.tbank.ru:443"
    grpc_target_sandbox: str = "sandbox-invest-public-api.tbank.ru:443"
    ca_bundle_path: Path = Path("config/certs/russian_trusted_ca.pem")
    insecure_tls_dev_only: bool = Field(
        default=False,
        description="Аварийный люк: полное отключение проверки TLS. Запрещён в LIVE.",
    )
    max_subscriptions_per_channel: int = Field(default=300, ge=1, le=300)
    orderbook_depth: int = Field(default=20, ge=1, le=50)


class StorageSettings(BaseModel):
    """Хранилище: лимиты ресурсов и политика архивации."""

    data_dir: Path = Path("data")
    duckdb_memory_limit_mb: int = Field(default=1536, ge=128, le=16384)
    duckdb_threads: int = Field(default=2, ge=1, le=16)
    disk_usage_threshold_pct: float = Field(default=0.8, gt=0.0, le=1.0)
    hot_retention_days: int = Field(default=1, ge=1)
    warm_retention_days: int = Field(default=180, ge=1)
    archive_batch_size: int = Field(default=50_000, ge=100)


class RiskDefaultsSettings(BaseModel):
    """Значения по умолчанию для риск-модуля (seed operational-конфига)."""

    commission_rate: float = Field(default=0.003, ge=0.0, le=0.1)
    min_viable_target_multiplier: float = Field(default=2.0, ge=1.0)
    max_risk_per_trade_pct: float = Field(default=0.01, gt=0.0, le=0.05)
    hypothesis_min_sample_size: int = Field(default=30, ge=5)
    walk_forward_confirmation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    default_max_holding_hours: int = Field(default=72, ge=1)


class WebSettings(BaseModel):
    """Web GUI."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    session_secret: SecretStr = SecretStr("dev-only-insecure-secret")


class Settings(BaseSettings):
    """Корневая модель bootstrap-конфигурации."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    execution_mode: ExecutionMode = ExecutionMode.SANDBOX
    log_level: LogLevel = LogLevel.INFO
    log_json: bool = False

    tbank: TBankSettings
    storage: StorageSettings = StorageSettings()
    risk_defaults: RiskDefaultsSettings = RiskDefaultsSettings()
    web: WebSettings = WebSettings()

    @model_validator(mode="after")
    def _forbid_insecure_tls_in_live(self) -> Settings:
        if self.execution_mode is ExecutionMode.LIVE and self.tbank.insecure_tls_dev_only:
            raise ValueError(
                "insecure_tls_dev_only запрещён в режиме LIVE: "
                "уберите флаг или смените execution_mode"
            )
        return self

    @model_validator(mode="after")
    def _forbid_missing_account_in_live(self) -> Settings:
        if self.execution_mode is ExecutionMode.LIVE and not self.tbank.account_id.strip():
            raise ValueError("account_id обязателен для режима LIVE")
        return self

    @model_validator(mode="after")
    def _forbid_placeholder_token_in_live(self) -> Settings:
        if self.execution_mode is not ExecutionMode.LIVE:
            return self
        token = self.tbank.api_token.get_secret_value().strip()
        if not token or token == "changeme":
            raise ValueError("api_token не заполнен — боевой режим невозможен")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            KeyringSettingsSource(settings_cls),
        )


_settings_instance: Settings | None = None


def load_settings(**overrides: Any) -> Settings:
    """Загружает конфиг. Кешируется: процесс живёт с одной конфигурацией.

    ``overrides`` имеют наивысший приоритет (используются в точке входа и тестах).
    """
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings(**overrides)
    return _settings_instance


def reset_settings_cache() -> None:
    """Сбрасывает кеш. Нужно только тестам."""
    global _settings_instance
    _settings_instance = None


def target_for_mode(settings: Settings) -> str:
    """Возвращает gRPC-target для текущего контура исполнения."""
    if settings.execution_mode is ExecutionMode.LIVE:
        return settings.tbank.grpc_target_live
    return settings.tbank.grpc_target_sandbox
