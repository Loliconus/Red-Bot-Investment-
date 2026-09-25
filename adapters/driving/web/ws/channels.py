"""Имена каналов и строгая валидация динамических подписок."""

from __future__ import annotations

import re
from enum import StrEnum


class Channel(StrEnum):
    SYSTEM_MODE = "system.mode"
    SYSTEM_RESOURCES = "system.resources"
    SYSTEM_TASKS = "system.tasks"
    DASHBOARD_DECISIONS = "dashboard.decisions"
    JOURNAL_HYPOTHESES = "journal.hypotheses"
    DB_ADMIN_JOBS = "db_admin.jobs"
    SECURITY_AUDIT = "security.audit"
    CONTROL_LOGS = "control.logs"
    SYSTEM_NOTIFICATIONS = "system.notifications"


DYNAMIC_CHANNEL = re.compile(r"^(chart\.[\w-]{1,80}|backtest\.[a-f0-9-]{8,36})$")


def valid_channel(value: str) -> bool:
    return value in Channel._value2member_map_ or bool(DYNAMIC_CHANNEL.fullmatch(value))
