"""Структурированное логирование с обязательным маскированием секретов.

Любое поле, имя которого содержит маркер секрета, заменяется на ``***``
до записи. Это защита на случай, если в лог случайно попадёт весь объект
настроек, а не отдельное безопасное поле.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

SENSITIVE_MARKERS = ("token", "password", "secret", "api_key", "authorization")
REDACTED = "***REDACTED***"

#: Имена полей, которые маскируются целиком и по точному совпадению.
SENSITIVE_KEYS = frozenset({"api_token", "session_secret", "password", "passwd", "pwd"})


def redact_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Маскирует значения секретных полей в структурированном событии."""
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in SENSITIVE_KEYS or any(marker in lowered for marker in SENSITIVE_MARKERS):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "info", *, json_logs: bool = False) -> None:
    """Настраивает structlog поверх стандартного logging. Вызывается один раз при старте."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
