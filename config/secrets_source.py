"""Кастомный источник секретов: системное хранилище учётных данных ОС.

Боевой токен не должен лежать в текстовом файле проекта. Библиотека ``keyring``
даёт единый интерфейс к Windows Credential Manager / macOS Keychain /
Linux Secret Service.

Источник подключается в ``Settings.settings_customise_sources`` с приоритетом
ниже явных аргументов, переменных окружения и ``.env`` — то есть ``.env``
остаётся удобством локальной разработки, а при наличии значения в keyring оно
подхватывается, если нигде выше не задано.

Хранилище опционально: если keyring недоступен (например, headless-сервер без
Secret Service), источник молча возвращает пустой словарь, а не роняет старт.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

logger = logging.getLogger(__name__)

SERVICE_NAME = "red-bot"
TOKEN_KEY = "tbank_api_token"
SESSION_SECRET_KEY = "web_session_secret"


def _get_password(key: str) -> str | None:
    try:
        import keyring
    except Exception:  # noqa: BLE001 - keyring может отсутствовать
        logger.debug("keyring недоступен, системное хранилище секретов не используется")
        return None
    try:
        value = keyring.get_password(SERVICE_NAME, key)
    except Exception as exc:  # noqa: BLE001 - сбой бэкенда ОС
        logger.warning("Не удалось прочитать секрет %s из keyring: %s", key, exc)
        return None
    return value or None


class KeyringSettingsSource(PydanticBaseSettingsSource):
    """Читает секреты из системного хранилища ОС."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        tbank: dict[str, Any] = {}
        web: dict[str, Any] = {}

        token = _get_password(TOKEN_KEY)
        if token:
            tbank["api_token"] = token

        session_secret = _get_password(SESSION_SECRET_KEY)
        if session_secret:
            web["session_secret"] = session_secret

        if tbank:
            result["tbank"] = tbank
        if web:
            result["web"] = web
        return result


def store_token(token: str) -> None:
    """Положить токен в системное хранилище (вызывается из CLI/GUI)."""
    import keyring

    keyring.set_password(SERVICE_NAME, TOKEN_KEY, token)


__all__ = ["SERVICE_NAME", "KeyringSettingsSource", "SettingsBase", "store_token"]

# Совместимость с типизацией pydantic-settings: базовый класс для настроек.
SettingsBase = BaseSettings
