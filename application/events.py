"""Шина событий уровня приложения.

Домен только **описывает** факты (``core/domain/events.py``); доставка — здесь.
Ошибка обработчика не должна валить торговый цикл, поэтому каждый обработчик
вызывается изолированно: исключение логируется и уходит в нотификатор,
остальные подписчики продолжают работу.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.ports.persistence import DecisionRecord

Handler = Callable[[Any], Awaitable[None]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionRecorded:
    record: DecisionRecord


@dataclass(frozen=True, slots=True)
class DecisionCycleCompleted:
    """Проход по корзине завершён: полный отчёт доступен в ``report``."""

    report: Any


class EventBus:
    """Простая in-process шина: ``тип события -> список обработчиков``."""

    __slots__ = ("_handlers", "_tasks")

    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def subscribers_count(self, event_type: type | None = None) -> int:
        if event_type is not None:
            return len(self._handlers.get(event_type, ()))
        return sum(len(v) for v in self._handlers.values())

    async def publish(self, event: Any) -> None:
        """Рассылает событие подписчикам. Исключения изолируются."""
        event_type = type(event)
        for handler in list(self._handlers.get(event_type, ())):
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Ошибка обработчика %s для события %s",
                    getattr(handler, "__name__", handler),
                    event_type.__name__,
                )

    def publish_fire_and_forget(self, event: Any) -> asyncio.Task[None]:
        """Неблокирующая отправка: создаёт задачу, не дожидаясь результата."""
        task = asyncio.create_task(self.publish(event), name=f"event:{type(event).__name__}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def clear(self) -> None:
        self._handlers.clear()


async def noop_handler(event: Any) -> None:
    """Обработчик-заглушка для тестов."""
