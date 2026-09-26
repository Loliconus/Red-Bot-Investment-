"""Симулятор исполнения для бэктеста.

Имитирует биржу: заявки исполняются по текущей цене с проскальзыванием и
комиссией. Зачем это нужно, если есть песочница: песочница даёт реальный
сетевой контур, но не умеет «прогнать три года истории за секунды» и не имеет
исторических данных. Симулятор закрывает ровно эту задачу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from core.domain.entities import (
    Instrument,
    OrderResult,
    OrderState,
    PortfolioState,
    Position,
    TradePlan,
)
from core.domain.enums import OrderStatus

logger = structlog.get_logger(__name__)

ZERO = Decimal("0")


@dataclass(slots=True)
class SimulatedFill:
    order_id: str
    plan_id: UUID | None
    instrument_uid: str
    lots: int
    price: Decimal
    side: str
    reason: str
    filled_at: datetime


@dataclass(slots=True)
class SimulatedBroker:
    """Симулятор: мгновенное исполнение по цене с проскальзыванием."""

    slippage_pct: Decimal = Decimal("0.0005")
    commission_rate: Decimal = Decimal("0.0005")
    account_id: str = "backtest"
    price_provider: Any = None
    fills: list[SimulatedFill] = field(default_factory=list)
    orders: dict[str, OrderState] = field(default_factory=dict)
    _counter: int = 0
    _balance: Decimal = Decimal("1000000")
    _accounts: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "id": "sim-sandbox-01",
                "name": "Основной счёт в песочнице",
                "status": 1,
                "type": 1,
                "is_current": True,
            }
        ]
    )

    async def get_portfolio(self) -> PortfolioState | None:
        positions = await self.get_open_positions()
        pos_val = sum((p.market_value(p.average_entry) for p in positions), ZERO)
        return PortfolioState(
            account_id=self.account_id,
            total_value=self._balance + pos_val,
            available_cash=self._balance,
            positions_value=pos_val,
            updated_at=datetime.now(tz=UTC),
        )

    async def get_sandbox_accounts(self) -> list[dict[str, Any]]:
        for acc in self._accounts:
            acc["is_current"] = acc["id"] == self.account_id
        return list(self._accounts)

    async def open_sandbox_account(self, name: str = "Новый счёт в песочнице") -> str:
        new_id = f"sim-sandbox-{len(self._accounts) + 1:02d}"
        self._accounts.append(
            {"id": new_id, "name": name, "status": 1, "type": 1, "is_current": False}
        )
        return new_id

    async def close_sandbox_account(self, account_id: str) -> None:
        self._accounts = [a for a in self._accounts if a["id"] != account_id]

    async def sandbox_pay_in(
        self, account_id: str, amount: Decimal, currency: str = "rub"
    ) -> Decimal:
        self._balance += amount
        return self._balance

    def set_price(self, instrument_uid: str, price: Decimal) -> None:
        """Задаёт текущую цену инструмента (обычно из реплея)."""
        if self.price_provider is None:
            self.price_provider = {}
        self.price_provider[instrument_uid] = price

    def _price_for(self, instrument: Instrument) -> Decimal:
        if self.price_provider and instrument.uid in self.price_provider:
            return Decimal(self.price_provider[instrument.uid])
        msg = f"Не задана цена для {instrument.ticker}: вызовите set_price перед исполнением"
        raise ValueError(msg)

    def _next_id(self) -> str:
        self._counter += 1
        return f"sim-{self._counter:06d}"

    async def place_order(self, plan: TradePlan, quantity: int) -> OrderResult:
        price = self._price_for(plan.instrument)
        buy_price = price * (Decimal("1") + self.slippage_pct)
        order_id = self._next_id()

        self.fills.append(
            SimulatedFill(
                order_id=order_id,
                plan_id=plan.id,
                instrument_uid=plan.instrument.uid,
                lots=quantity,
                price=buy_price,
                side="buy",
                reason="entry",
                filled_at=datetime.now(tz=UTC),
            )
        )
        state = OrderState(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_lots=quantity,
            filled_price=buy_price,
        )
        self.orders[order_id] = state
        logger.debug("simulated_fill", order_id=order_id, lots=quantity, price=str(buy_price))
        return OrderResult(
            order_id=order_id,
            client_order_id=str(plan.id),
            status=OrderStatus.FILLED,
            filled_lots=quantity,
            filled_price=buy_price,
            message="Симулированное исполнение",
        )

    async def cancel_order(self, order_id: str) -> None:
        state = self.orders.get(order_id)
        if state is not None:
            self.orders[order_id] = OrderState(
                order_id=order_id,
                status=OrderStatus.CANCELLED,
                filled_lots=state.filled_lots,
                filled_price=state.filled_price,
            )

    async def get_order_status(self, order_id: str) -> OrderState:
        return self.orders.get(
            order_id,
            OrderState(order_id=order_id, status=OrderStatus.UNKNOWN),
        )

    async def close_position(self, position: Position, reason: str) -> OrderResult:
        price = self._price_for(position.instrument)
        sell_price = price * (Decimal("1") - self.slippage_pct)
        order_id = self._next_id()
        lots = max(position.lots, 1)

        self.fills.append(
            SimulatedFill(
                order_id=order_id,
                plan_id=position.linked_plan_id,
                instrument_uid=position.instrument.uid,
                lots=lots,
                price=sell_price,
                side="sell",
                reason=reason,
                filled_at=datetime.now(tz=UTC),
            )
        )
        state = OrderState(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_lots=lots,
            filled_price=sell_price,
        )
        self.orders[order_id] = state
        return OrderResult(
            order_id=order_id,
            client_order_id=f"{position.linked_plan_id}-close",
            status=OrderStatus.FILLED,
            filled_lots=lots,
            filled_price=sell_price,
            message=f"Закрытие: {reason}",
        )

    async def get_open_positions(self) -> list[Position]:
        """Открытые позиции по журналу симулированных сделок."""
        from collections import defaultdict

        net: dict[str, int] = defaultdict(int)
        entry_price: dict[str, Decimal] = {}
        instrument_by_uid: dict[str, Instrument] = {}
        plan_by_uid: dict[str, UUID] = {}

        for fill in self.fills:
            uid = fill.instrument_uid
            signed = fill.lots if fill.side == "buy" else -fill.lots
            net[uid] += signed
            entry_price.setdefault(uid, fill.price)
            plan_by_uid.setdefault(uid, fill.plan_id or UUID(int=0))
            if fill.instrument_uid not in instrument_by_uid:
                instrument_by_uid[uid] = _instrument_stub(uid)

        positions: list[Position] = []
        for uid, lots in net.items():
            if lots <= 0:
                continue
            positions.append(
                Position(
                    instrument=instrument_by_uid[uid],
                    quantity=lots * instrument_by_uid[uid].lot_size,
                    average_entry=entry_price[uid],
                    opened_at=datetime.now(tz=UTC),
                    linked_plan_id=plan_by_uid[uid],
                )
            )
        return positions

    async def get_instrument(self, uid: str) -> Instrument | None:
        return _instrument_stub(uid)

    async def aclose(self) -> None:
        self.fills.clear()
        self.orders.clear()

    def total_commission(self) -> Decimal:
        """Суммарная комиссия по всем симулированным сделкам."""
        total = ZERO
        for fill in self.fills:
            notional = fill.price * Decimal(fill.lots)
            total += notional * self.commission_rate
        return total


def _instrument_stub(uid: str) -> Instrument:
    return Instrument(uid=uid, ticker=uid, lot_size=1)
