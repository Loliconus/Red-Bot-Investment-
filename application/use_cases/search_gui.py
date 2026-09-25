"""Поиск сущностей для командной палитры (только сконфигурированные данные)."""

from __future__ import annotations

from application.composition import AppContext


async def search_gui(context: AppContext, prefix: str) -> list[dict[str, str]]:
    query = prefix.strip().lower()[:80]
    if len(query) < 2:
        return []
    results: list[dict[str, str]] = []
    for item in context.instruments:
        if query in item.ticker.lower() or item.uid.lower().startswith(query):
            results.append(
                {
                    "label": f"{item.ticker} · график",
                    "url": f"/chart/{item.uid}",
                    "category": "ИНСТРУМЕНТ",
                    "hint": item.uid[:12],
                }
            )
    plans = await context.repository.get_open_trade_plans()
    for plan in plans:
        if str(plan.id).startswith(query) or query in plan.instrument.ticker.lower():
            results.append(
                {
                    "label": f"План {plan.instrument.ticker} · {str(plan.id)[:8]}",
                    "url": f"/chart/{plan.instrument.uid}",
                    "category": "TRADE PLAN",
                    "hint": str(plan.id)[:8],
                }
            )
    return results[:20]
