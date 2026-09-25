"""Аналитические ручки: прогон гипотез, метрики, отчёты."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from adapters.driving.web.dependencies import (
    ContextDep,
    SessionDep,
    require_session,
)
from application.use_cases.generate_daily_report import run_self_analysis
from core.journal.advisory import build_advice
from core.journal.hypothesis_engine import walk_forward_efficiency

router = APIRouter(
    prefix="/api/analysis",
    tags=["analysis"],
    dependencies=[Depends(require_session)],
)


class WalkForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(
        default="exit_efficiency < 0.4",
        description="Условие отбора сделок (поддерживаются простые сравнения)",
    )
    train_ratio: float = Field(default=0.6, ge=0.3, le=0.9)
    since_days: int = Field(default=180, ge=7, le=3650)


class WalkForwardResponse(BaseModel):
    matched_in_sample: int
    matched_out_of_sample: int
    efficiency: float | None
    verdict: str
    advice: list[str]


_SUPPORTED_CONDITIONS: dict[str, Any] = {
    "exit_efficiency < 0.4": lambda r: r.exit_efficiency < Decimal("0.4"),
    "exit_efficiency < 0.6": lambda r: r.exit_efficiency < Decimal("0.6"),
    "post_exit_drift_pct > 0.02": lambda r: r.post_exit_drift_pct > Decimal("0.02"),
    "mae > 0.02": lambda r: r.mae > Decimal("0.02"),
    "verdict == overstayed": lambda r: r.verdict.value == "overstayed",
    "realized_pnl > 0": lambda r: r.realized_pnl > Decimal("0"),
}


@router.post("/walk-forward", response_model=WalkForwardResponse)
async def walk_forward(
    context: ContextDep,
    _session: SessionDep,
    payload: WalkForwardRequest,
) -> WalkForwardResponse:
    """Прогон walk-forward проверки по накопленной истории сделок.

    Efficiency выше 0.5 считается приемлемой, ниже 0.3 — признак переобучения.
    """
    condition = _SUPPORTED_CONDITIONS.get(payload.condition)
    if condition is None:
        raise HTTPException(
            status_code=400,
            detail=f"Неподдерживаемое условие. Доступны: {sorted(_SUPPORTED_CONDITIONS)}",
        )

    since = context.clock.now() - timedelta(days=payload.since_days)
    reviews = await context.repository.get_trade_history(None, since=since)
    if not reviews:
        raise HTTPException(status_code=404, detail="Нет истории сделок за период")

    efficiency = walk_forward_efficiency(reviews, condition, train_ratio=payload.train_ratio)
    matched_is = sum(1 for r in reviews[: int(len(reviews) * payload.train_ratio)] if condition(r))
    matched_oos = sum(1 for r in reviews[int(len(reviews) * payload.train_ratio) :] if condition(r))

    if efficiency is None:
        verdict = "недостаточно данных для оценки"
    elif efficiency >= Decimal("0.5"):
        verdict = "приемлемо: гипотеза выдерживает проверку на новых данных"
    elif efficiency >= Decimal("0.3"):
        verdict = "погранично: требуется больше сделок"
    else:
        verdict = "переобучение: на out-of-sample результат не подтверждается"

    hypotheses = await context.repository.list_hypotheses()
    advice = [a.render() for a in build_advice(hypotheses)]

    return WalkForwardResponse(
        matched_in_sample=matched_is,
        matched_out_of_sample=matched_oos,
        efficiency=float(efficiency) if efficiency is not None else None,
        verdict=verdict,
        advice=advice,
    )


@router.post("/self-analysis")
async def self_analysis(context: ContextDep, _session: SessionDep) -> dict[str, Any]:
    """Запускает формирование гипотез по истории сделок."""
    proposals = await run_self_analysis(context)
    return {
        "hypotheses": [
            {
                "id": str(h.id),
                "text": h.text,
                "status": h.status.value,
                "confidence": str(h.confidence),
                "sample_size": h.sample_size,
                "walk_forward_efficiency": (
                    str(h.walk_forward_efficiency)
                    if h.walk_forward_efficiency is not None
                    else None
                ),
            }
            for h in proposals
        ]
    }
