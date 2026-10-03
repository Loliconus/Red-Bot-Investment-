"""Безопасное управление торговым процессом через GUI.

Soft pause не останавливает монитор позиций; hard stop останавливает все циклы
и требует отдельного подтверждения на HTTP-границе И в этом use case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.composition import AppContext

STOP_PHRASE = "ОСТАНОВИТЬ"


@dataclass(frozen=True, slots=True)
class ControlState:
    status: str
    mode: str
    uptime_seconds: int
    started_at: datetime | None
    last_restart_at: datetime | None
    last_restart_reason: str | None
    soft_paused: bool
    hard_stop_latched: bool
    pause_reason: str
    tasks: list[dict[str, Any]]
    broker_adapter: str
    market_adapter: str
    bootstrap: dict[str, str]


def get_control_state(context: AppContext) -> ControlState:
    scheduler = context.scheduler
    running = scheduler is not None and scheduler.is_running
    paused = bool(context.kill_switch and context.kill_switch.is_engaged)
    tasks = list(scheduler.stats().values()) if scheduler is not None else []
    if scheduler is not None and not tasks:
        tasks = [
            {
                "name": spec.name,
                "status": "STOPPED",
                "last_tick_at": None,
                "interval_seconds": spec.interval_seconds,
                "failures": 0,
                "manual_restarts": 0,
                "can_restart": spec.name != "position_monitor",
            }
            for spec in scheduler.tasks
        ]
    status = "STOPPED"
    if running:
        status = (
            "PAUSED"
            if paused
            else ("DEGRADED" if any(task["status"] == "DEGRADED" for task in tasks) else "RUNNING")
        )
    since = context.last_restart_at or context.started_at
    now = context.clock.now()
    uptime = max(int((now - since).total_seconds()), 0) if running and since else 0
    settings = context.settings
    return ControlState(
        status=status,
        mode=context.execution_mode.value,
        uptime_seconds=uptime,
        started_at=context.started_at,
        last_restart_at=context.last_restart_at,
        last_restart_reason=context.last_restart_reason,
        soft_paused=paused,
        hard_stop_latched=context.hard_stop_latched,
        pause_reason=context.kill_switch.reason if paused and context.kill_switch else "",
        tasks=tasks,
        broker_adapter=type(context.broker).__name__,
        market_adapter=type(context.market_data).__name__,
        bootstrap={
            "token_source": "keyring / окружение (значение скрыто)",
            "env": context.execution_mode.value,
            "tls": "небезопасный dev-режим"
            if settings.tbank.insecure_tls_dev_only
            else "проверка включена",
            "memory_limit_mb": str(
                context.storage_memory_limit_mb or settings.storage.duckdb_memory_limit_mb
            ),
            "threads": str(settings.storage.duckdb_threads),
            "config_hint": (
                "Режим и счёт следующего запуска меняются в разделе «Настройки запуска»; "
                "секреты и TLS остаются под отдельными ограничениями."
            ),
        },
    )


async def set_soft_pause(context: AppContext, *, engaged: bool, reason: str) -> None:
    if context.kill_switch is None:
        raise ValueError("Kill switch не инициализирован")
    if context.hard_stop_latched:
        raise ValueError(
            "Hard Stop активен: снять его из GUI невозможно; требуется рестарт приложения"
        )
    if engaged:
        if context.kill_switch.is_engaged and context.kill_switch.initiated_by != "gui-soft-pause":
            raise ValueError("Блокировка инициирована системой или риском; снять её из GUI нельзя")
        await context.kill_switch.engage(reason, initiated_by="gui-soft-pause")
    else:
        if context.kill_switch.is_engaged and context.kill_switch.initiated_by != "gui-soft-pause":
            raise ValueError("Блокировка инициирована системой или риском; снять её из GUI нельзя")
        if context.kill_switch.is_engaged:
            context.kill_switch.release()


async def stop_bot(context: AppContext, *, confirmation: str, hard_stop: bool = False) -> None:
    if confirmation != STOP_PHRASE:
        raise ValueError(f"Для остановки введите {STOP_PHRASE}")
    if context.scheduler is None or not context.scheduler.is_running:
        raise ValueError("Торговый процесс уже остановлен")
    if hard_stop:
        context.hard_stop_latched = True
        if context.kill_switch is not None:
            await context.kill_switch.engage("Hard Stop из GUI", initiated_by="gui-hard-stop")
    context.scheduler.stop()
    if context.scheduler_task is not None:
        # Пока задача реально не завершилась, восстановление БД недоступно.
        await asyncio.wait_for(context.scheduler_task, timeout=30)


async def start_bot(context: AppContext, *, confirmation: str = "") -> None:
    if context.scheduler is None:
        raise ValueError("Scheduler не подключён к Web GUI")
    if context.hard_stop_latched:
        raise ValueError("Hard Stop не отключается из GUI: нужен полный рестарт приложения")
    if context.restart_required:
        raise ValueError("После восстановления или смены счёта требуется рестарт всего приложения")
    if context.scheduler.is_active:
        raise ValueError("Процесс уже запущен или ещё завершает остановку")
    if context.execution_mode.value == "live" and confirmation != "ЗАПУСТИТЬ LIVE":
        raise ValueError("Для LIVE введите ЗАПУСТИТЬ LIVE")
    context.last_restart_at = context.clock.now()
    context.last_restart_reason = "ручной запуск из GUI"
    context.scheduler_task = asyncio.create_task(context.scheduler.run(), name="bot-scheduler")
    await asyncio.sleep(0)


def restart_scheduler_task(context: AppContext, name: str) -> None:
    if context.scheduler is None:
        raise ValueError("Scheduler не подключён")
    context.scheduler.restart_task(name)
