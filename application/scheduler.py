"""Управляемый планировщик торговых циклов.

Задачи изолированы в ResilientLoop, исключение отдельного цикла не сбивает
защиту позиций. Ручной перезапуск делает *дополнительный тик после текущего*,
не прерывая выполняемую операцию (и никогда не предлагается для position_monitor).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)
BASE_RESTART_DELAY = 1.0
MAX_RESTART_DELAY = 60.0
Cycle = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class ResilientLoop:
    name: str
    cycle: Cycle
    interval_seconds: float
    alert: Callable[[str], Awaitable[None]] | None = None
    run_immediately: bool = True
    delay: float = BASE_RESTART_DELAY
    iterations: int = 0
    failures: int = 0
    manual_restarts: int = 0
    last_tick_at: datetime | None = None
    last_error: str | None = None
    status: str = "WAITING"
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                if self.iterations > 0 or self.run_immediately:
                    await self._run_once()
                if stop_event.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=self.interval_seconds)
                if self.wakeup.is_set():
                    self.wakeup.clear()
        finally:
            self.status = "STOPPED"

    async def _run_once(self) -> None:
        self.status = "RUNNING"
        try:
            await self.cycle()
            self.last_error = None
            self.status = "HEALTHY"
            self.delay = BASE_RESTART_DELAY
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            self.status = "DEGRADED"
            self.last_error = type(exc).__name__  # не раскрывать секреты в исключениях
            logger.exception("Ошибка в цикле %s, перезапуск через %.1f с", self.name, self.delay)
            if self.alert is not None:
                try:
                    await self.alert(f"Цикл {self.name} упал: {type(exc).__name__}")
                except Exception:  # noqa: BLE001 - алерт не должен ломать цикл
                    logger.warning("Не удалось отправить алерт по циклу %s", self.name)
            await asyncio.sleep(self.delay)
            self.delay = min(self.delay * 2, MAX_RESTART_DELAY)
        finally:
            self.iterations += 1
            self.last_tick_at = datetime.now(tz=UTC)


@dataclass(slots=True)
class TaskSpec:
    name: str
    cycle: Cycle
    interval_seconds: float
    run_immediately: bool = True


@dataclass(slots=True)
class Scheduler:
    tasks: list[TaskSpec] = field(default_factory=list)
    alert: Callable[[str], Awaitable[None]] | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    loops: list[ResilientLoop] = field(default_factory=list)
    _running: bool = False

    def add(self, spec: TaskSpec) -> None:
        if any(task.name == spec.name for task in self.tasks):
            raise ValueError(f"Задача {spec.name} уже зарегистрирована")
        self.tasks.append(spec)

    def add_task(self, name: str, cycle: Cycle, interval_seconds: float) -> None:
        self.add(TaskSpec(name=name, cycle=cycle, interval_seconds=interval_seconds))

    def stop(self) -> None:
        self.stop_event.set()
        for loop in self.loops:
            loop.wakeup.set()

    @property
    def is_running(self) -> bool:
        return self._running and not self.stop_event.is_set()

    @property
    def is_active(self) -> bool:
        """True пока хоть один цикл выполняется/завершает работу после stop()."""
        return self._running

    def restart_task(self, name: str) -> None:
        if name in {"position_monitor", "monitor"}:
            raise PermissionError("Монитор позиций перезапускается только полной остановкой бота")
        if not self.is_running:
            raise ValueError("Scheduler остановлен")
        loop = next((item for item in self.loops if item.name == name), None)
        if loop is None:
            raise ValueError("Задача не найдена")
        loop.manual_restarts += 1
        loop.wakeup.set()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("Scheduler уже запущен")
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
        self._running = True
        try:
            async with asyncio.TaskGroup() as group:
                for loop in self.loops:
                    group.create_task(loop.run(self.stop_event), name=f"loop:{loop.name}")
        finally:
            self._running = False

    def stats(self) -> dict[str, dict[str, Any]]:
        return {
            loop.name: {
                "name": loop.name,
                "status": loop.status,
                "iterations": loop.iterations,
                "failures": loop.failures,
                "manual_restarts": loop.manual_restarts,
                "last_tick_at": loop.last_tick_at,
                "last_error": loop.last_error,
                "interval_seconds": loop.interval_seconds,
                "can_restart": loop.name not in {"position_monitor", "monitor"},
            }
            for loop in self.loops
        }
