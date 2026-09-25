"""Юнит-тесты bootstrap-конфигурации: fail-fast и безопасность секретов."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config.enums import ExecutionMode
from config.logging_config import redact_processor
from config.settings import Settings, load_settings, target_for_mode


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "execution_mode": ExecutionMode.SANDBOX,
        "tbank": {"api_token": "token", "account_id": "acc"},
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_settings_accepts_minimal_sandbox_config() -> None:
    settings = _settings()
    assert settings.execution_mode is ExecutionMode.SANDBOX
    assert settings.tbank.api_token.get_secret_value() == "token"


def test_settings_forbids_unknown_keys() -> None:
    """Опечатка в ``.env`` — это ошибка, а не молчаливый игнор."""
    with pytest.raises(ValidationError, match="extra"):
        _settings(rebot_log_level="debug")


def test_settings_forbids_insecure_tls_in_live() -> None:
    with pytest.raises(ValidationError, match="insecure_tls_dev_only"):
        _settings(
            execution_mode=ExecutionMode.LIVE,
            tbank={
                "api_token": "real-token",
                "account_id": "acc",
                "insecure_tls_dev_only": True,
            },
        )


def test_settings_allows_insecure_tls_in_sandbox() -> None:
    settings = _settings(tbank={"api_token": "t", "account_id": "a", "insecure_tls_dev_only": True})
    assert settings.tbank.insecure_tls_dev_only


def test_settings_requires_account_in_live() -> None:
    with pytest.raises(ValidationError, match="account_id"):
        _settings(execution_mode=ExecutionMode.LIVE, tbank={"api_token": "real"})


def test_settings_rejects_placeholder_token_in_live() -> None:
    with pytest.raises(ValidationError, match="api_token"):
        _settings(
            execution_mode=ExecutionMode.LIVE,
            tbank={"api_token": "changeme", "account_id": "acc"},
        )


def test_settings_accepts_real_token_in_live() -> None:
    settings = _settings(
        execution_mode=ExecutionMode.LIVE,
        tbank={"api_token": "real-secret-token", "account_id": "acc"},
    )
    assert settings.execution_mode is ExecutionMode.LIVE


def test_settings_validates_storage_limits() -> None:
    with pytest.raises(ValidationError):
        _settings(storage={"duckdb_memory_limit_mb": 1})


def test_settings_validates_risk_defaults() -> None:
    with pytest.raises(ValidationError):
        _settings(risk_defaults={"max_risk_per_trade_pct": 0.9})


def test_target_for_mode_switches_grpc_endpoint() -> None:
    live = _settings(
        execution_mode=ExecutionMode.LIVE, tbank={"api_token": "real-token", "account_id": "acc"}
    )
    sandbox = _settings()
    assert "sandbox" in target_for_mode(sandbox)
    assert "sandbox" not in target_for_mode(live)


def test_load_settings_is_cached() -> None:
    first = load_settings(
        execution_mode=ExecutionMode.SANDBOX, tbank={"api_token": "t", "account_id": "a"}
    )
    second = load_settings(execution_mode=ExecutionMode.BACKTEST)
    assert first is second


def test_default_paths_and_values() -> None:
    settings = _settings()
    assert isinstance(settings.storage.data_dir, Path)
    assert settings.storage.disk_usage_threshold_pct == 0.8
    assert settings.risk_defaults.min_viable_target_multiplier == 2.0
    assert settings.risk_defaults.hypothesis_min_sample_size == 30


# ------------------------------------------------------------------ логи
def test_redact_processor_masks_secrets() -> None:
    event = {
        "api_token": "секрет",
        "tbank_password": "секрет",
        "authorization": "Bearer xyz",
        "message": "обычное сообщение",
    }
    redacted = redact_processor(None, "info", dict(event))
    assert redacted["api_token"] == "***REDACTED***"
    assert redacted["tbank_password"] == "***REDACTED***"
    assert redacted["authorization"] == "***REDACTED***"
    assert redacted["message"] == "обычное сообщение"
