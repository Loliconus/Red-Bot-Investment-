"""Первичное заполнение БД: инструменты, конфиг v1, бенчмарк.

Запускается один раз при старте на пустой базе. Дальнейшая жизнь параметров
происходит в БД через GUI, а не в исходниках.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from config.seed_defaults import (
    DEFAULT_CONFLUENCE_WEIGHTS,
    DEFAULT_INSTRUMENTS,
)
from core.domain.entities import Instrument, StrategyConfig

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)


async def bootstrap_database(ctx: AppContext) -> StrategyConfig:
    """Создаёт стартовый конфиг и инструменты, если их ещё нет."""
    existing = await ctx.repository.get_active_strategy_config()
    if existing is not None:
        return existing

    risks = ctx.settings.risk_defaults
    config = StrategyConfig(
        version=1,
        risk_per_trade_pct=Decimal(str(risks.max_risk_per_trade_pct)),
        min_viable_target_multiplier=Decimal(str(risks.min_viable_target_multiplier)),
        commission_rate=Decimal(str(risks.commission_rate)),
        max_holding_hours=risks.default_max_holding_hours,
        max_position_notional=Decimal("500000"),
        confluence_threshold=Decimal("0.3"),
        confluence_weights=dict(DEFAULT_CONFLUENCE_WEIGHTS),
    )
    await ctx.repository.save_strategy_config(config)

    for raw in DEFAULT_INSTRUMENTS:
        try:
            instrument = await ctx.market_data.resolve_instrument(
                str(raw["ticker"]), str(raw["class_code"])
            )
        except Exception:  # noqa: BLE001 — на старте сеть может быть недоступна
            logger.warning(
                "instrument_unavailable",
                ticker=raw["ticker"],
                reason="не удалось разрешить через API",
            )
            continue
        instrument.is_benchmark = bool(raw["is_benchmark"])
        await ctx.repository.save_instrument(instrument)

    logger.info("database_bootstrapped", config_version=config.version)
    return config


def seed_instrument(
    uid: str, ticker: str, lot_size: int, *, is_benchmark: bool = False
) -> Instrument:
    """Создаёт сущность инструмента без обращения к API (для тестов и бэктеста)."""
    return Instrument(
        uid=uid,
        ticker=ticker,
        lot_size=lot_size,
        is_benchmark=is_benchmark,
    )
