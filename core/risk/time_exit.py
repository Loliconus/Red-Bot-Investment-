"""Time exit — временной выход (TTL идеи).

Формализует кейс «сижу три дня, ничего не происходит, а деньги заморожены».
Жёсткий стоп и тейк по цене не спасают от позиции, которая просто стоит:
она не приносит прибыли, но продолжает нести риск и отнимать место в портфеле.

TTL задаётся как ``max_holding_time`` в самом ``TradePlan`` (по умолчанию
72 часа) и отсчитывается от момента создания идеи.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.domain.entities import TradePlan

ZERO_SECONDS = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeExitResult:
    """Результат проверки TTL."""

    triggered: bool
    held_for: timedelta
    remaining: timedelta
    expires_at: datetime
    reason: str


def remaining_time(plan: TradePlan, now: datetime) -> timedelta:
    """Сколько времени осталось держать идею. Может быть отрицательным."""
    return plan.expires_at() - now


def held_for(plan: TradePlan, now: datetime) -> timedelta:
    return now - plan.created_at


def check_time_exit(plan: TradePlan, now: datetime) -> TimeExitResult:
    """Проверяет TTL идеи.

    Время берётся из аргумента, а не из ``datetime.now()`` — иначе бэктест и
    юнит-тесты были бы недетерминированы.
    """
    expires_at = plan.expires_at()
    remaining = expires_at - now
    held = now - plan.created_at
    triggered = remaining <= timedelta(seconds=ZERO_SECONDS)

    if triggered:
        reason = (
            f"TTL идеи истёк: держали {_humanize(held)}, лимит {_humanize(plan.max_holding_time)}"
        )
    else:
        reason = f"осталось {_humanize(remaining)} до TTL"

    return TimeExitResult(
        triggered=triggered,
        held_for=held,
        remaining=remaining,
        expires_at=expires_at,
        reason=reason,
    )


def is_expired(plan: TradePlan, now: datetime) -> bool:
    return check_time_exit(plan, now).triggered


def _humanize(delta: timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"
