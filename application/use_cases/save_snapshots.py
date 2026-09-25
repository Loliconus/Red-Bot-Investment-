"""Сохранение снапшотов.

Каждый цикл анализа — даже «ничего не делать» — фиксируется. Это фундамент
самоанализа: без полной истории увиденного невозможно потом ответить, почему
бот принял именно такое решение.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from core.journal.snapshots import DecisionSnapshot, MarketSnapshot

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)


async def save_market_snapshot(ctx: AppContext, snapshot: MarketSnapshot) -> UUID:
    snapshot_id = await ctx.repository.save_market_snapshot(snapshot)
    logger.debug("market_snapshot_saved", id=str(snapshot_id))
    return snapshot_id


async def save_decision_snapshot(ctx: AppContext, snapshot: DecisionSnapshot) -> UUID:
    snapshot_id = await ctx.repository.save_decision_snapshot(snapshot)
    logger.debug("decision_snapshot_saved", id=str(snapshot_id))
    return snapshot_id


async def save_both(
    ctx: AppContext,
    market: MarketSnapshot,
    decision: DecisionSnapshot,
) -> tuple[UUID, UUID]:
    return (
        await save_market_snapshot(ctx, market),
        await save_decision_snapshot(ctx, decision),
    )
