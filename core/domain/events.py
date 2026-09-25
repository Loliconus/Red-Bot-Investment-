"""Доменные события: факты, произошедшие внутри ядра.

Это **описание факта**, а не механизм доставки. Доставкой подписчикам занимается
``application/events.py`` (EventBus). Разделение намеренное: ядро не знает, кто
и как получит событие.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Базовое доменное событие."""

    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlanProposed(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    entry_price: Decimal
    confluence_score: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class TradePlanRejected(DomainEvent):
    plan_id: UUID | None
    instrument_uid: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderSubmitted(DomainEvent):
    plan_id: UUID
    client_order_id: str
    exchange_order_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFilled(DomainEvent):
    plan_id: UUID
    exchange_order_id: str
    filled_lots: int
    filled_price: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionOpened(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    quantity: int
    average_entry: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class HardStopTriggered(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    stop_price: Decimal
    current_price: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class ThesisInvalidated(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    rule_code: str
    description: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeExitTriggered(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    held_for_seconds: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetReached(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    target_price: Decimal
    exit_price: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionClosed(DomainEvent):
    plan_id: UUID
    instrument_uid: str
    exit_price: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class KillSwitchEngaged(DomainEvent):
    reason: str
    initiated_by: str
