"""Порт времени.

Любой код в ``core/``, которому нужно текущее время (TTL-проверки,
``max_holding_time``), обязан принимать ``ClockPort`` через конструктор или
аргумент, а не звать ``datetime.now()`` напрямую. Это единственный способ
детерминированно тестировать стратегию и прогонять бэктест на исторических
датах без искажения «текущим временем машины».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime:
        """Текущий момент. Всегда tz-aware UTC."""
        ...


class SystemClock:
    """Реализация на системном времени."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    """Управляемое время для тестов и бэктеста."""

    __slots__ = ("_current",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            msg = "FrozenClock требует tz-aware datetime (UTC)"
            raise ValueError(msg)
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        self._current += delta
        return self._current

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            msg = "FrozenClock требует tz-aware datetime (UTC)"
            raise ValueError(msg)
        self._current = moment
