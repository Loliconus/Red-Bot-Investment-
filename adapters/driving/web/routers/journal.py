"""Журнал: мысли, сделки, гипотезы, снапшоты."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from adapters.driving.web.dependencies import (
    ContextDep,
    SessionDep,
    require_session,
)
from adapters.driving.web.render import render_page, render_partial
from adapters.driving.web.schemas import (
    DecisionResponse,
    HypothesisApplyRequest,
    HypothesisResponse,
    OkResponse,
    TradeReviewResponse,
)
from application.use_cases.approve_hypothesis import CONFIRM_PHRASE, approve_hypothesis
from application.use_cases.gui_views import journal_view

page_router = APIRouter(tags=["journal-page"], dependencies=[Depends(require_session)])


@page_router.get("/journal")
async def journal_page(
    request: Request,
    context: ContextDep,
    days: int = 180,
    instrument_uid: str | None = None,
) -> Any:
    return render_page(
        request,
        "pages/journal.html",
        title="Журнал и самоанализ",
        section="journal",
        data={
            "journal": await journal_view(
                context, days=min(max(days, 1), 3650), instrument_uid=instrument_uid
            ),
            "days": days,
            "instrument_uid": instrument_uid or "",
        },
    )


@page_router.get("/journal/hypotheses/{hypothesis_id}/review")
async def review_hypothesis(request: Request, context: ContextDep, hypothesis_id: UUID) -> Any:
    entries = await context.repository.list_hypotheses()
    target = next((h for h in entries if h.id == hypothesis_id), None)
    if target is None or target.status.value != "confirmed":
        raise HTTPException(status_code=404, detail="Гипотеза не подтверждена")
    return render_partial(
        request,
        "partials/hypothesis_confirm.html",
        {
            "hypothesis": target,
            "config_version": context.config.version,
            "confirm_phrase": CONFIRM_PHRASE,
        },
    )


@page_router.post("/journal/hypotheses/{hypothesis_id}/approve")
async def approve_form(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    hypothesis_id: UUID,
    confirmation: str = Form(default=""),
) -> Any:
    try:
        status = await approve_hypothesis(context, hypothesis_id, confirmation)
    except (LookupError, ValueError) as exc:
        return render_partial(
            request,
            "partials/hypothesis_confirm.html",
            {"error": str(exc), "hypothesis": None, "confirm_phrase": CONFIRM_PHRASE},
            status_code=409,
        )
    request.state.audit_after = {"hypothesis_id": str(hypothesis_id), "status": status}
    await request.app.state.hub.publish(
        "journal.hypotheses",
        "hypothesis.updated",
        {
            "id": str(hypothesis_id),
            "status": status,
        },
    )
    return render_partial(
        request,
        "partials/hypothesis_confirm.html",
        {"message": "Одобрено. Числовые параметры стратегии не менялись автоматически."},
    )


router = APIRouter(
    prefix="/api/journal",
    tags=["journal"],
    dependencies=[Depends(require_session)],
)


@router.get("/decisions", response_model=list[DecisionResponse])
async def recent_decisions(context: ContextDep, *, since_days: int = 1) -> list[Any]:
    """Последние решения. История читается напрямую из read-only SQL."""
    since = context.clock.now() - timedelta(days=max(since_days, 1))
    rows = await context.repository.execute_readonly(
        "SELECT id, decision, confluence_score, reasoning, risk_check_passed, "
        "risk_check_reason, thought_text, created_at FROM decision_snapshots "
        "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 200",
        [since],
    )
    import json

    return [
        DecisionResponse(
            id=UUID(row[0]),
            decision=row[1],
            confluence_score=row[2],
            reasoning=json.loads(row[3]) if isinstance(row[3], str) else row[3],
            risk_check_passed=bool(row[4]),
            risk_check_reason=row[5],
            thought_text=row[6],
            created_at=row[7],
        )
        for row in rows
    ]


@router.get("/trades", response_model=list[TradeReviewResponse])
async def trades(context: ContextDep, *, since_days: int = 30) -> list[TradeReviewResponse]:
    since = context.clock.now() - timedelta(days=max(since_days, 1))
    reviews = await context.repository.get_trade_history(None, since=since)
    return [
        TradeReviewResponse(
            trade_plan_id=r.trade_plan_id,
            verdict=r.verdict.value,
            entry_price=r.entry_price,
            exit_price=r.exit_price,
            mfe=r.mfe,
            mae=r.mae,
            exit_efficiency=r.exit_efficiency,
            post_exit_drift_pct=r.post_exit_drift_pct,
            realized_pnl=r.realized_pnl,
            closed_at=r.closed_at,
        )
        for r in reviews
    ]


@router.get("/hypotheses", response_model=list[HypothesisResponse])
async def hypotheses(context: ContextDep, *, status: str | None = None) -> list[HypothesisResponse]:
    items = await context.repository.list_hypotheses(status)
    return [
        HypothesisResponse(
            id=h.id,
            text=h.text,
            suggested_action=h.suggested_action,
            status=h.status.value,
            confidence=h.confidence,
            sample_size=h.sample_size,
            walk_forward_efficiency=h.walk_forward_efficiency,
            evidence=h.evidence,
        )
        for h in items
    ]


@router.post("/hypotheses/apply", response_model=OkResponse)
async def apply_hypothesis(
    context: ContextDep,
    _session: SessionDep,
    payload: HypothesisApplyRequest,
) -> OkResponse:
    """Применение гипотезы к боевому конфигу.

    Автоприменение **запрещено**: требуется явный флаг ``confirmed_by_user``.
    Гипотеза обязана быть в статусе CONFIRMED и пройти walk-forward.
    """
    if not payload.confirmed_by_user:
        raise HTTPException(
            status_code=400,
            detail="Автоприменение запрещено: выставьте confirmed_by_user=true",
        )

    try:
        await approve_hypothesis(context, payload.hypothesis_id, payload.confirmation)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return OkResponse(ok=True, detail=f"Гипотеза {payload.hypothesis_id} одобрена")


@router.get("/market-snapshots/{snapshot_id}")
async def market_snapshot(context: ContextDep, snapshot_id: str) -> Any:
    from uuid import UUID

    try:
        uid = UUID(snapshot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный id") from exc

    snapshot = await context.repository.get_market_snapshot(uid)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Снапшот не найден")
    return {
        "id": str(snapshot.id),
        "instrument_uid": snapshot.instrument_uid,
        "captured_at": snapshot.captured_at,
        "indicators": {
            tf.value: {k: str(v) for k, v in vals.items()}
            for tf, vals in snapshot.indicators.items()
        },
        "signals": {tf.value: dict(v) for tf, v in snapshot.signals.items()},
        "market_regime": {tf.value: r.value for tf, r in snapshot.market_regime.items()},
    }
