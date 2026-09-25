"""Ручки операционного конфига.

Операционный конфиг живёт в БД и правится из GUI — правка исходников под
«подбор параметров» запрещена хотя бы потому, что не оставляет следа.
Каждое изменение создаёт новую версию.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from adapters.driving.web.dependencies import (
    ContextDep,
    SessionDep,
    require_session,
)
from adapters.driving.web.schemas import (
    StrategyConfigResponse,
    StrategyConfigUpdate,
)
from application.use_cases.manage_risk import COUNTERTREND_PHRASE

router = APIRouter(
    prefix="/api/config",
    tags=["config"],
    dependencies=[Depends(require_session)],
)


@router.get("", response_model=StrategyConfigResponse)
async def get_config(context: ContextDep) -> StrategyConfigResponse:
    config = context.config
    return StrategyConfigResponse(
        version=config.version,
        risk_per_trade_pct=config.risk_per_trade_pct,
        min_viable_target_multiplier=config.min_viable_target_multiplier,
        commission_rate=config.commission_rate,
        max_holding_hours=config.max_holding_hours,
        max_position_notional=config.max_position_notional,
        confluence_threshold=config.confluence_threshold,
        confluence_weights=config.confluence_weights,
        allow_counter_trend=config.allow_counter_trend,
        daily_loss_limit_pct=config.daily_loss_limit_pct,
    )


@router.put("", response_model=StrategyConfigResponse)
async def update_config(
    context: ContextDep,
    _session: SessionDep,
    payload: StrategyConfigUpdate,
) -> StrategyConfigResponse:
    """Обновляет конфиг. Непереданные поля (``None``) не меняются."""
    from application.use_cases.update_strategy_config import update_strategy_config

    if (
        payload.allow_counter_trend
        and not context.config.allow_counter_trend
        and (payload.counter_trend_confirmation != COUNTERTREND_PHRASE)
    ):
        raise HTTPException(status_code=409, detail=f"Введите точно {COUNTERTREND_PHRASE}")
    try:
        updated = await update_strategy_config(
            context,
            **payload.model_dump(exclude_none=True, exclude={"counter_trend_confirmation"}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StrategyConfigResponse(
        version=updated.version,
        risk_per_trade_pct=updated.risk_per_trade_pct,
        min_viable_target_multiplier=updated.min_viable_target_multiplier,
        commission_rate=updated.commission_rate,
        max_holding_hours=updated.max_holding_hours,
        max_position_notional=updated.max_position_notional,
        confluence_threshold=updated.confluence_threshold,
        confluence_weights=updated.confluence_weights,
        allow_counter_trend=updated.allow_counter_trend,
        daily_loss_limit_pct=updated.daily_loss_limit_pct,
    )
