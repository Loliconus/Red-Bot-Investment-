"""Снимки системного состояния и ресурсы для status bar и WS."""

from __future__ import annotations

import shutil
from typing import Any

import psutil

from application.composition import AppContext
from application.use_cases.gui_control import get_control_state
from application.use_cases.gui_views import recent_decisions

_PROCESS = psutil.Process()
GIB = 1024**3
MIB = 1024**2


def _level(
    value: float | int | None, warning: float, critical: float, *, lower_is_worse: bool = False
) -> str:
    if value is None:
        return "unknown"
    if lower_is_worse:
        return "critical" if value < critical else "warning" if value < warning else "normal"
    return "critical" if value > critical else "warning" if value > warning else "normal"


async def resources(context: AppContext) -> dict[str, Any]:
    cpu = _PROCESS.cpu_percent(interval=None)
    ram_gui = _PROCESS.memory_info().rss
    ram_db = await context.repository.memory_used_bytes()
    try:
        data_dir = context.settings.storage.data_dir.resolve()
        disk_free = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent).free
    except OSError:
        disk_free = None
    limit = (
        context.storage_memory_limit_mb or context.settings.storage.duckdb_memory_limit_mb
    ) * MIB
    db_pct = ram_db / limit * 100 if ram_db is not None else None
    return {
        "cpu": cpu,
        "ram_gui": ram_gui,
        "ram_duckdb": ram_db,
        "ram_duckdb_limit": limit,
        "disk_free": disk_free,
        "latency": context.broker_latency_ms,
        "levels": {
            "cpu": _level(cpu, 70, 90),
            "ram_gui": _level(ram_gui, 500 * MIB, GIB),
            "ram_duckdb": _level(db_pct, 80, 95),
            "disk_free": _level(disk_free, 20 * GIB, 5 * GIB, lower_is_worse=True),
            "latency": _level(context.broker_latency_ms, 300, 1000),
        },
    }


async def channel_snapshot(context: AppContext, channel: str) -> dict[str, Any] | list[Any]:
    if channel == "system.mode":
        return {
            "mode": context.execution_mode.value,
            "since": context.started_at.isoformat() if context.started_at else None,
        }
    if channel == "system.resources":
        return await resources(context)
    if channel == "system.tasks":
        return get_control_state(context).tasks
    if channel == "dashboard.decisions":
        return await recent_decisions(context)
    if channel == "journal.hypotheses":
        hypotheses = await context.repository.list_hypotheses()
        return [
            {"id": str(item.id), "status": item.status.value, "text": item.text}
            for item in hypotheses
        ]
    if channel == "security.audit":
        entries = await context.repository.list_gui_audit(limit=20)
        return [
            {
                "id": str(item.id),
                "ts": item.ts.isoformat(),
                "section": item.section,
                "action": item.action,
                "outcome": item.outcome,
            }
            for item in entries
        ]
    if channel == "db_admin.jobs":
        return []
    if channel == "reasoning.scan":
        report = context.decision_scan_report
        return report.summary() if report is not None else {}
    if channel in {"control.logs", "system.notifications"}:
        return []
    if channel.startswith("chart.") or channel.startswith("backtest."):
        return []
    return {}
