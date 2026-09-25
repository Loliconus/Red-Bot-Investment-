"""Обновление операционного конфига из GUI.

Конфиг версионируется: каждое изменение создаёт **новую** версию, старые
остаются в БД. Это позволяет потом сопоставить результаты сделок с конфигом,
который действовал на момент входа, — без этого любой анализ «стало лучше или
хуже» превращается в догадки.

Соглашение: ``None`` означает «не менять».
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, TypeVar

import structlog

from core.domain.entities import StrategyConfig

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)


async def update_strategy_config(
    ctx: AppContext,
    *,
    risk_per_trade_pct: Decimal | None = None,
    confluence_threshold: Decimal | None = None,
    commission_rate: Decimal | None = None,
    min_viable_target_multiplier: Decimal | None = None,
    max_holding_hours: int | None = None,
    max_position_notional: Decimal | None = None,
    confluence_weights: dict[str, Decimal] | None = None,
    allow_counter_trend: bool | None = None,
    daily_loss_limit_pct: Decimal | None = None,
) -> StrategyConfig:
    """Создаёт новую версию конфига на основе активной."""
    stored = await ctx.repository.get_active_strategy_config()
    # БД ещё пуста — источником правды служит конфиг, уже загруженный в контекст.
    current = stored if stored is not None else ctx.config

    merged_weights = dict(current.confluence_weights)
    if confluence_weights:
        merged_weights.update(confluence_weights)

    updated = StrategyConfig(
        version=current.version + 1,
        risk_per_trade_pct=_pick(risk_per_trade_pct, current.risk_per_trade_pct),
        min_viable_target_multiplier=_pick(
            min_viable_target_multiplier, current.min_viable_target_multiplier
        ),
        commission_rate=_pick(commission_rate, current.commission_rate),
        max_holding_hours=_pick(max_holding_hours, current.max_holding_hours),
        max_position_notional=_pick(max_position_notional, current.max_position_notional),
        confluence_threshold=_pick(confluence_threshold, current.confluence_threshold),
        confluence_weights=merged_weights,
        allow_counter_trend=_pick(allow_counter_trend, current.allow_counter_trend),
        daily_loss_limit_pct=_pick(daily_loss_limit_pct, current.daily_loss_limit_pct),
    )

    await ctx.repository.save_strategy_config(updated)
    ctx.config = updated
    logger.info("strategy_config_updated", version=updated.version)
    return updated


T = TypeVar("T")


def _pick(value: T | None, default: T) -> T:
    """``None`` = «не менять»."""
    return default if value is None else value
