"""Торговые ручки: планы, позиции, принудительный анализ."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from adapters.driving.web.dependencies import (
    ContextDep,
    SessionDep,
    require_session,
)
from adapters.driving.web.schemas import (
    AnalyzeResponse,
    PositionResponse,
    TradePlanResponse,
)
from application.use_cases.make_decision import make_decision

router = APIRouter(
    prefix="/api/trading",
    tags=["trading"],
    dependencies=[Depends(require_session)],
)


@router.get("/plans", response_model=list[TradePlanResponse])
async def list_plans(context: ContextDep) -> list[TradePlanResponse]:
    plans = await context.repository.get_open_trade_plans()
    return [
        TradePlanResponse(
            id=plan.id,
            instrument=plan.instrument.uid,
            ticker=plan.instrument.ticker,
            status=plan.status.value,
            entry_price=plan.entry_price,
            hard_stop_price=plan.hard_stop_price,
            target_price=plan.target_price,
            risk_reward=plan.risk_reward_ratio,
            confluence_score=plan.thesis.confluence_score,
            created_at=plan.created_at,
            expires_at=plan.expires_at(),
            quantity_lots=plan.quantity_lots,
            rejection_reason=plan.rejection_reason,
        )
        for plan in plans
    ]


@router.get("/positions", response_model=list[PositionResponse])
async def list_positions(context: ContextDep) -> list[PositionResponse]:
    positions = await context.broker.get_open_positions()
    return [
        PositionResponse(
            instrument=p.instrument.uid,
            ticker=p.instrument.ticker,
            quantity=p.quantity,
            lots=p.lots,
            average_entry=p.average_entry,
        )
        for p in positions
    ]


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(context: ContextDep, _session: SessionDep) -> AnalyzeResponse:
    """Прогоняет цикл принятия решения по всей корзине без исполнения ордеров."""
    results: list[dict[str, Any]] = []
    for instrument in context.tradable_instruments:
        try:
            outcome = await make_decision(context, instrument)
        except Exception as exc:  # noqa: BLE001 - GUI не должен падать из-за одной бумаги
            results.append({"instrument": instrument.ticker, "error": str(exc)})
            continue
        results.append(
            {
                "instrument": instrument.ticker,
                "decision": outcome.decision.value,
                "confluence_score": outcome.market_snapshot
                and outcome.decision_snapshot.confluence_score,
                "reason": outcome.reason,
                "lots": outcome.sizing.lots if outcome.sizing else 0,
            }
        )
    return AnalyzeResponse(results=results)


@router.get("/plans/{plan_id}", response_model=TradePlanResponse)
async def get_plan(context: ContextDep, plan_id: str) -> TradePlanResponse:
    from uuid import UUID

    try:
        uid = UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный plan_id") from exc

    plan = await context.repository.get_trade_plan(uid)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return TradePlanResponse(
        id=plan.id,
        instrument=plan.instrument.uid,
        ticker=plan.instrument.ticker,
        status=plan.status.value,
        entry_price=plan.entry_price,
        hard_stop_price=plan.hard_stop_price,
        target_price=plan.target_price,
        risk_reward=plan.risk_reward_ratio,
        confluence_score=plan.thesis.confluence_score,
        created_at=plan.created_at,
        expires_at=plan.expires_at(),
        quantity_lots=plan.quantity_lots,
        rejection_reason=plan.rejection_reason,
    )
