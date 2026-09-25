"""Исполнение ордеров и закрытие позиций.

Идемпотентность: ключ ``client_order_id`` генерируется **до** сетевого вызова и
сохраняется адаптером. Повторная отправка того же ключа не создаёт вторую сделку
— это единственная защита от «двойной заявки» при обрыве соединения.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from core.domain.entities import OrderResult, Position, TradePlan
from core.domain.enums import ExitReason, OrderStatus, TradePlanStatus
from core.domain.events import (
    OrderFilled,
    OrderSubmitted,
    PositionOpened,
    TradePlanRejected,
)
from core.risk.position_sizing import SizingResult

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)

#: Ограничение API на длину клиентского идентификатора ордера.
MAX_CLIENT_ORDER_ID_LENGTH = 36
#: Длина стабильного префикса от UUID плана.
PLAN_KEY_LENGTH = 12

ZERO = Decimal("0")


def build_client_order_id(plan: TradePlan, *, suffix: str = "") -> str:
    """Детерминированный ключ идемпотентности на основе ``plan.id``.

    Один и тот же план всегда даёт один и тот же ключ, поэтому повторная
    отправка после таймаута соединения не создаёт вторую сделку: биржа
    отвергнет дубль по тому же ``order_id`` из запроса.
    """
    # UUID плана целиком (36 символов) не оставляет места для суффикса,
    # поэтому берём стабильный префикс: он всё равно уникален в пределах счёта.
    prefix = plan.id.hex[:PLAN_KEY_LENGTH]
    raw = f"{prefix}-{suffix}" if suffix else prefix
    return raw[:MAX_CLIENT_ORDER_ID_LENGTH]


async def execute_plan(
    ctx: AppContext,
    plan: TradePlan,
    sizing: SizingResult,
) -> OrderResult:
    """Отправляет ордер на вход."""
    if sizing.is_empty:
        reason = f"нулевой размер позиции: {sizing.reason}"
        plan.reject(reason, closed_at=ctx.clock.now())
        await ctx.repository.save_trade_plan(plan)
        await ctx.event_bus.publish(
            TradePlanRejected(
                occurred_at=ctx.clock.now(),
                plan_id=plan.id,
                instrument_uid=plan.instrument.uid,
                reason=reason,
            )
        )
        return OrderResult(
            order_id="",
            client_order_id="",
            status=OrderStatus.REJECTED,
            message=reason,
        )

    if ctx.kill_switch is not None and ctx.kill_switch.is_engaged:
        reason = "kill switch активен — ордера заблокированы"
        plan.reject(reason, closed_at=ctx.clock.now())
        await ctx.repository.save_trade_plan(plan)
        return OrderResult(
            order_id="",
            client_order_id="",
            status=OrderStatus.REJECTED,
            message=reason,
        )

    plan.mark_pending()
    plan.quantity_lots = sizing.lots
    await ctx.repository.save_trade_plan(plan)

    result = await ctx.broker.place_order(plan, quantity=sizing.lots)

    await ctx.event_bus.publish(
        OrderSubmitted(
            occurred_at=ctx.clock.now(),
            plan_id=plan.id,
            client_order_id=result.client_order_id,
            exchange_order_id=result.order_id,
        )
    )

    if result.status is OrderStatus.FILLED:
        plan.activate()
        await ctx.repository.save_trade_plan(plan)
        await ctx.event_bus.publish(
            OrderFilled(
                occurred_at=ctx.clock.now(),
                plan_id=plan.id,
                exchange_order_id=result.order_id,
                filled_lots=result.filled_lots,
                filled_price=result.filled_price or plan.entry_price,
            )
        )
        await ctx.event_bus.publish(
            PositionOpened(
                occurred_at=ctx.clock.now(),
                plan_id=plan.id,
                instrument_uid=plan.instrument.uid,
                quantity=result.filled_lots * plan.instrument.lot_size,
                average_entry=result.filled_price or plan.entry_price,
            )
        )
        if ctx.notifier is not None:
            await ctx.notifier.send(
                f"Открыта позиция {plan.instrument.ticker}: "
                f"{result.filled_lots} лотов по {result.filled_price}"
            )

    logger.info(
        "order_submitted",
        plan_id=str(plan.id),
        client_order_id=result.client_order_id,
        status=result.status.value,
    )
    return result


async def close_plan(
    ctx: AppContext,
    plan: TradePlan,
    reason: ExitReason,
) -> OrderResult | None:
    """Закрывает позицию по плану."""
    position = Position(
        instrument=plan.instrument,
        quantity=plan.quantity_lots * plan.instrument.lot_size,
        average_entry=plan.entry_price,
        opened_at=plan.created_at,
        linked_plan_id=plan.id,
    )
    if position.quantity <= 0:
        return None

    result = await ctx.broker.close_position(position, reason=reason.value)
    if ctx.notifier is not None:
        await ctx.notifier.send(
            f"Закрытие {plan.instrument.ticker} по причине {reason.value}: {result.message}"
        )
    return result


async def cancel_plan(ctx: AppContext, plan: TradePlan, order_id: str) -> None:
    """Отменяет выставленный, но не исполненный ордер."""
    await ctx.broker.cancel_order(order_id)
    plan.close(TradePlanStatus.CANCELLED, closed_at=ctx.clock.now())
    await ctx.repository.save_trade_plan(plan)
    logger.info("order_cancelled", plan_id=str(plan.id), order_id=order_id)
