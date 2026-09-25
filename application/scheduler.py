"""Планировщик торговых циклов.

Два требования, из которых следует устройство модуля:

1. **Живучесть.** Падение одного цикла (обрыв стрима, таймаут API) не должно
   ронять весь процесс: цикл перезапускается с экспоненциальной задержкой.
2. **Остановка по сигналу.** ``Ctrl+C`` обязан останавливать всё корректно:
   задачи снимаются через ``asyncio.TaskGroup``, а не «убиваются».

Задачи запускаются через ``asyncio.TaskGroup`` — при ошибке в любой задаче
группа отменяет остальные, поэтому обработка ошибок происходит внутри цикла.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Базовая задержка перед перезапуском упавшего цикла.
BASE_RESTART_DELAY = 1.0
#: Максимальная задержка (чтобы не ждать минуты между попытками).
MAX_RESTART_DELAY = 60.0

Cycle = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class ResilientLoop:
    """Бесконечный цикл задачи с перезапуском при ошибке."""

    name: str
    cycle: Cycle
    interval_seconds: float
    alert: Callable[[str], Awaitable[None]] | None = None
    run_immediately: bool = True
    delay: float = BASE_RESTART_DELAY
    iterations: int = 0
    failures: int = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        """Работает до установки ``stop_event``."""
        while not stop_event.is_set():
            if self.iterations > 0 or self.run_immediately:
                await self._run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

    async def _run_once(self) -> None:
        try:
            await self.cycle()
            self.iterations += 1
            self.delay = BASE_RESTART_DELAY
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            self.iterations += 1
            logger.exception("Ошибка в цикле %s, перезапуск через %.1f с", self.name, self.delay)
            if self.alert is not None:
                try:
                    await self.alert(f"Цикл {self.name} упал: {exc}")
                except Exception:  # noqa: BLE001 - алерт не должен ломать цикл
                    logger.warning("Не удалось отправить алерт по циклу %s", self.name)
            await asyncio.sleep(self.delay)
            self.delay = min(self.delay * 2, MAX_RESTART_DELAY)


@dataclass(slots=True)
class TaskSpec:
    """Описание периодической задачи."""

    name: str
    cycle: Cycle
    interval_seconds: float
    run_immediately: bool = True


@dataclass(slots=True)
class Scheduler:
    """Набор периодических задач c общей точкой остановки."""

    tasks: list[TaskSpec] = field(default_factory=list)
    alert: Callable[[str], Awaitable[None]] | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    loops: list[ResilientLoop] = field(default_factory=list)

    def add(self, spec: TaskSpec) -> None:
        self.tasks.append(spec)

    def add_task(self, name: str, cycle: Cycle, interval_seconds: float) -> None:
        self.add(TaskSpec(name=name, cycle=cycle, interval_seconds=interval_seconds))

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def is_running(self) -> bool:
        return not self.stop_event.is_set()

    async def run(self) -> None:
        """Запускает все задачи в одной группе и ждёт остановки."""
        self.stop_event = asyncio.Event()
        self.loops = [
            ResilientLoop(
                name=spec.name,
                cycle=spec.cycle,
                interval_seconds=spec.interval_seconds,
                alert=self.alert,
                run_immediately=spec.run_immediately,
            )
            for spec in self.tasks
        ]

        async with asyncio.TaskGroup() as group:
            for loop in self.loops:
                group.create_task(loop.run(self.stop_event), name=f"loop:{loop.name}")

    def stats(self) -> dict[str, dict[str, Any]]:
        """Статистика по циклам для GUI."""
        return {
            loop.name: {
                "iterations": loop.iterations,
                "failures": loop.failures,
                "interval_seconds": loop.interval_seconds,
            }
            for loop in self.loops
        }
