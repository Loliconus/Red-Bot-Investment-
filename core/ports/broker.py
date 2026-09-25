"""Порт исполнения ордеров.

Единственное место в проекте, физически способное отправить ордер на биржу.
Каждая реализация обязана проверять ``account_id`` против
``settings.tbank.account_id`` (managed_account_id) и падать при несовпадении.

Сам контракт этого не типизирует: ядро не знает о существовании счетов вообще —
это деталь инфраструктуры. Проверка живёт в адаптере.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.domain.entities import Instrument, OrderResult, OrderState, Position, TradePlan


@runtime_checkable
class OrderExecutionPort(Protocol):
    """Выставление, отмена и статус ордеров."""

    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult:
        """Выставляет long-ордер. ``quantity`` — количество ЛОТОВ.

        Адаптер обязан сгенерировать ключ идемпотентности, сохранить его до
        сетевого вызова и вернуть как ``OrderResult.client_order_id``.
        """
        ...

    async def cancel_order(self, order_id: str) -> None: ...

    async def get_order_status(self, order_id: str) -> OrderState: ...

    async def close_position(self, position: Position, reason: str) -> OrderResult:
        """Закрывает позицию рыночным ордером. Используется механизмами защиты."""
        ...

    async def get_open_positions(self) -> list[Position]: ...

    async def get_instrument(self, uid: str) -> Instrument | None:
        """Справочная информация об инструменте из контура (лотность, шаг цены)."""
        ...
