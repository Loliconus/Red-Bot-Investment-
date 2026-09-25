"""Буфер очищенных structlog/stdlib сообщений для пульта (без секретов)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import UTC, datetime
from typing import Any

from adapters.driving.web.ws.hub import BroadcastHub

SENSITIVE_TEXT = re.compile(
    r"(?i)(\b(?:api_token|authorization|password|secret|account_id|bearer)\b\s*[:=]\s*)\S+"
)


class GuiLogHandler(logging.Handler):
    def __init__(self, hub: BroadcastHub, loop: asyncio.AbstractEventLoop, secret: str) -> None:
        super().__init__()
        self._hub = hub
        self._loop = loop
        self._secrets = {secret} if secret else set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.rows: deque[dict[str, Any]] = deque(maxlen=2000)

    def redact_secret(self, secret: str) -> None:
        if secret:
            self._secrets.add(secret)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            for secret in self._secrets:
                message = message.replace(secret, "[скрыто]")
            message = SENSITIVE_TEXT.sub(r"\1[скрыто]", message)[:2000]
            row = {
                "ts": datetime.now(tz=UTC).isoformat(),
                "level": record.levelname,
                "module": record.name[:100],
                "message": message,
            }
            self._loop.call_soon_threadsafe(self._append, row)
        except (RuntimeError, ValueError):
            pass  # при остановке event loop новые логи не доставляются

    def _append(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        task = asyncio.create_task(self._hub.publish("control.logs", "log.line", row))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def recent(self, *, module: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.rows
            if module is None or module in str(row["module"]) or module in str(row["message"])
        ]
        return rows[-min(limit, 2000) :]
