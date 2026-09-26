"""Подготовка read models для HTML и WS без доступа GUI к driven-адаптерам."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from application.composition import AppContext
from core.analysis.fibonacci import fibonacci_levels, find_swing
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries
from core.ports.persistence import DecisionRecord


def decision_view(record: DecisionRecord, context: AppContext) -> dict[str, Any]:
    snapshot = record.snapshot
    ticker = next(
        (i.ticker for i in context.instruments if i.uid == record.instrument_uid),
        record.instrument_uid or "—",
    )
    return {
        "id": str(snapshot.id),
        "instrument_uid": record.instrument_uid,
        "ticker": ticker,
        "decision": snapshot.decision.value,
        "confluence_score": str(snapshot.confluence_score),
        "risk_check_passed": snapshot.risk_check_passed,
        "risk_check_reason": snapshot.risk_check_reason or "",
        "thought_text": snapshot.thought_text,
        "created_at": snapshot.created_at.isoformat(),
        "plan_id": str(snapshot.trade_plan_id) if snapshot.trade_plan_id else None,
        "reasoning": [
            {
                "module": step.module,
                "signal": step.signal,
                "weight": str(step.weight),
                "comment": step.comment,
            }
            for step in snapshot.reasoning_chain
        ],
    }


async def recent_decisions(context: AppContext, *, limit: int = 50) -> list[dict[str, Any]]:
    records = await context.repository.list_recent_decisions(limit)
    return [decision_view(record, context) for record in records]


async def dashboard_view(context: AppContext, *, period: str = "session") -> dict[str, Any]:
    now = context.clock.now()
    cutoff = {
        "session": now - timedelta(days=1),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }.get(period, now - timedelta(days=1))
    plans, records, hypotheses, reviews = await asyncio.gather(
        context.repository.get_open_trade_plans(),
        context.repository.list_recent_decisions(35),
        context.repository.list_hypotheses(),
        context.repository.get_trade_history(None, since=cutoff),
    )
    price_tasks = [asyncio.create_task(_last_price(context, p.instrument, now)) for p in plans]
    prices = await asyncio.gather(*price_tasks) if price_tasks else []
    priority: list[dict[str, Any]] = []
    cards = []
    for plan, price in zip(plans, prices, strict=True):
        ttl = max(int((plan.expires_at() - now).total_seconds()), 0)
        if ttl < 6 * 3600:
            priority.append(
                {
                    "urgency_score": 90 - min(ttl // 600, 35),
                    "type": "ttl",
                    "ticker": plan.instrument.ticker,
                    "text": "Истекает TTL плана",
                    "detail": f"{ttl // 3600}ч {(ttl // 60) % 60}м",
                }
            )
        if price is not None and price > plan.hard_stop_price:
            proximity = (price - plan.hard_stop_price) / price
            if proximity <= Decimal("0.02"):
                priority.append(
                    {
                        "urgency_score": 100 - int(proximity * 1000),
                        "type": "stop",
                        "ticker": plan.instrument.ticker,
                        "text": "Цена близко к hard stop",
                        "detail": str(price),
                    }
                )
        cards.append(
            {
                "id": str(plan.id),
                "ticker": plan.instrument.ticker,
                "uid": plan.instrument.uid,
                "status": plan.status.value,
                "direction": "LONG",
                "entry": str(plan.entry_price),
                "price": str(price) if price is not None else None,
                "stop": str(plan.hard_stop_price),
                "target": str(plan.target_price),
                "ttl_seconds": ttl,
                "score": str(plan.thesis.confluence_score),
                "reasoning": [
                    {
                        "module": step.module,
                        "signal": step.signal,
                        "weight": str(step.weight),
                        "comment": step.comment,
                    }
                    for step in plan.thesis.reasoning_chain
                ],
            }
        )
    for hypothesis in hypotheses:
        if hypothesis.status.value == "confirmed":
            priority.append(
                {
                    "urgency_score": 70,
                    "type": "review",
                    "ticker": "HYP",
                    "text": "Гипотеза готова к ревью",
                    "detail": hypothesis.text[:100],
                }
            )
    try:
        disk = shutil.disk_usage(_existing_data_path(context))
        if disk.free < 20 * 1024**3:
            priority.append(
                {
                    "urgency_score": 95 if disk.free < 5 * 1024**3 else 60,
                    "type": "disk",
                    "ticker": "ДИСК",
                    "text": "Свободное место ниже порога",
                    "detail": f"{disk.free / 1024**3:.1f} GB",
                }
            )
    except OSError:
        pass
    balance = context.portfolio.total_value if context.portfolio else None
    return {
        "priority": sorted(priority, key=lambda item: item["urgency_score"], reverse=True),
        "plans": cards,
        "decisions": [decision_view(record, context) for record in records],
        "portfolio": {
            "account_id": context.active_account_id,
            "balance": str(balance) if balance is not None else None,
            "realized": str(sum((r.realized_pnl for r in reviews), Decimal("0"))),
            "unrealized": None,  # нет свежей оценки всех позиций — не показываем 0
            "positions": [
                {
                    "ticker": p.instrument.ticker,
                    "lots": p.lots,
                    "average_entry": str(p.average_entry),
                }
                for p in await _positions(context)
            ],
            "period": period,
        },
        "benchmark": "IMOEX — справочно, не торгуется",
    }


async def _positions(context: AppContext) -> list[Any]:
    try:
        return await asyncio.wait_for(context.broker.get_open_positions(), timeout=0.5)
    except Exception:  # noqa: BLE001
        return []


async def _last_price(context: AppContext, instrument: Any, now: datetime) -> Decimal | None:
    try:
        candles = await asyncio.wait_for(
            context.market_data.get_candles(
                instrument, Timeframe.M1, from_=now - timedelta(minutes=15), to=now
            ),
            timeout=0.35,
        )
    except (TimeoutError, OSError, ValueError):
        return None
    return candles[-1].close if candles else None


def _existing_data_path(context: AppContext) -> Path:
    path = context.settings.storage.data_dir.resolve()
    return path if path.exists() else path.parent


async def chart_view(context: AppContext, uid: str, timeframe: Timeframe) -> dict[str, Any]:
    instrument = next((i for i in context.instruments if i.uid == uid), None)
    if instrument is None:
        raise ValueError("Инструмент не найден")
    now = context.clock.now()
    days = {Timeframe.D1: 365, Timeframe.H1: 30, Timeframe.M1: 2}[timeframe]
    candles = await context.market_data.get_candles(
        instrument, timeframe, from_=now - timedelta(days=days), to=now
    )
    records = await context.repository.list_recent_decisions(100)
    latest = next((decision_view(r, context) for r in records if r.instrument_uid == uid), None)
    try:
        orderbook = await asyncio.wait_for(
            context.market_data.get_orderbook(instrument, depth=10), timeout=0.4
        )
        bids = [{"price": str(p.price), "quantity": p.quantity} for p in orderbook.bids[:5]]
        asks = [{"price": str(p.price), "quantity": p.quantity} for p in orderbook.asks[:5]]
    except (TimeoutError, OSError, RuntimeError):
        bids, asks = [], []
    swing = find_swing(CandleSeries(timeframe=timeframe, candles=tuple(candles)))
    fib = {name: str(value) for name, value in fibonacci_levels(*swing).items()} if swing else {}
    benchmark: list[dict[str, Any]] = []
    if context.benchmark is not None and candles:
        try:
            benchmarks = await asyncio.wait_for(
                context.market_data.get_candles(
                    context.benchmark, timeframe, from_=now - timedelta(days=days), to=now
                ),
                timeout=0.4,
            )
            if benchmarks and benchmarks[0].close > 0:
                base = benchmarks[0].close
                benchmark = [
                    {
                        "time": int(c.timestamp.timestamp()),
                        "value": str((c.close / base * Decimal("100")).quantize(Decimal("0.01"))),
                    }
                    for c in benchmarks[-600:]
                ]
        except (TimeoutError, OSError, ValueError):
            pass
    plans = await context.repository.get_open_trade_plans()
    markers = [
        {
            "time": int(p.created_at.timestamp()),
            "price": str(p.entry_price),
            "plan_id": str(p.id),
            "status": p.status.value,
        }
        for p in plans
        if p.instrument.uid == uid
    ]
    return {
        "uid": uid,
        "ticker": instrument.ticker,
        "timeframe": timeframe.value,
        "fibonacci": fib,
        "benchmark": benchmark,
        "markers": markers,
        "bars": [
            {
                "time": int(c.timestamp.timestamp()),
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": c.volume,
            }
            for c in candles[-600:]
        ],
        "reasoning": latest,
        "bids": bids,
        "asks": asks,
    }


async def journal_view(
    context: AppContext, *, days: int = 180, instrument_uid: str | None = None
) -> dict[str, Any]:
    instrument = next((i for i in context.instruments if i.uid == instrument_uid), None)
    reviews, hypotheses = await asyncio.gather(
        context.repository.get_trade_history(
            instrument, since=context.clock.now() - timedelta(days=days)
        ),
        context.repository.list_hypotheses(),
    )
    efficiency_bins = [0] * 10
    drift_bins = [0] * 10
    for r in reviews:
        efficiency_bins[min(max(int(r.exit_efficiency * 10), 0), 9)] += 1
        drift_bins[min(max(int((r.post_exit_drift_pct + Decimal("0.10")) * 50), 0), 9)] += 1
    return {
        "efficiency_bins": efficiency_bins,
        "drift_bins": drift_bins,
        "hist_max": max([*efficiency_bins, *drift_bins, 1]),
        "trades": [
            {
                "id": str(r.trade_plan_id),
                "verdict": r.verdict.value,
                "entry": str(r.entry_price),
                "exit": str(r.exit_price),
                "exit_efficiency": str(r.exit_efficiency),
                "post_exit_drift_pct": str(r.post_exit_drift_pct),
                "pnl": str(r.realized_pnl),
                "closed_at": r.closed_at.isoformat(),
            }
            for r in reversed(reviews)
        ],
        "hypotheses": [
            {
                "id": str(h.id),
                "text": h.text,
                "status": h.status.value,
                "sample": h.sample_size,
                "confidence": str(h.confidence),
                "efficiency": str(h.walk_forward_efficiency)
                if h.walk_forward_efficiency is not None
                else None,
            }
            for h in hypotheses
        ],
    }


async def decision_detail(context: AppContext, decision_id: UUID) -> dict[str, Any] | None:
    records = await context.repository.list_recent_decisions(500)
    return next((decision_view(r, context) for r in records if r.snapshot.id == decision_id), None)
