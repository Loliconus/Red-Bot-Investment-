"""Порт внешних алертов.

Опциональный: ``AppContext.notifier`` может быть ``None``, если Telegram
не настроен.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotificationPort(Protocol):
    async def send(self, message: str, *, level: str = "info") -> None:
        """Отправляет сообщение. Ошибка доставки не должна ронять торговый цикл."""
        ...

    async def send_critical(self, message: str) -> None:
        """Критическое событие: срабатывание kill switch, потеря стрима, лимит убытка."""
        ...
