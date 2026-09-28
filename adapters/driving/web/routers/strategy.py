"""Стратегия: веса confluence, порог входа и структура мультитаймфреймового ТА.

Редактируется численный «мозг» решения. Каждое сохранение — новая версия
``strategy_configs`` (юзкейс ``update_strategy_config``), поэтому история
сделок всегда сопоставима с действовавшими правилами.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from adapters.driving.web.dependencies import ContextDep, SessionDep, require_session
from adapters.driving.web.render import render_page, render_partial
from application.use_cases.update_strategy_config import update_strategy_config

router = APIRouter(tags=["strategy"], dependencies=[Depends(require_session)])

#: Официальные модули confluence-скоринга (см. core/strategy/setup_scanner).
MODULE_LABELS: tuple[tuple[str, str], ...] = (
    ("regime_alignment", "Режим рынка (D1)"),
    ("trend_d1", "Тренд дневки (EMA/SMA)"),
    ("setup_h1", "Сетап часовика (RSI/MACD/BB)"),
    ("fibonacci", "Уровни Фибоначчи"),
    ("volume", "Объём (OBV/VWAP)"),
    ("relative_strength", "Сила против IMOEX"),
    ("orderbook", "Стакан заявок"),
)


def _scoring_view(context: Any, *, message: str = "", error: str = "") -> dict[str, Any]:
    config = context.config
    weights = [
        {
            "module": module,
            "label": label,
            "weight": str(config.confluence_weights.get(module, Decimal("0"))),
        }
        for module, label in MODULE_LABELS
    ]
    other = [
        {"module": module, "label": module, "weight": str(weight)}
        for module, weight in config.confluence_weights.items()
        if module not in dict(MODULE_LABELS)
    ]
    return {
        "version": config.version,
        "threshold": str(config.confluence_threshold),
        "weights": [*weights, *other],
        "message": message,
        "error": error,
    }


@router.get("/strategy")
async def page(request: Request, context: ContextDep) -> Any:
    return render_page(
        request,
        "pages/strategy.html",
        title="Стратегия",
        section="strategy",
        data={
            "scoring": _scoring_view(context),
            "config": {
                "commission_rate": str(context.config.commission_rate),
                "min_viable_target_multiplier": str(context.config.min_viable_target_multiplier),
                "max_holding_hours": context.config.max_holding_hours,
                "max_position_notional": str(context.config.max_position_notional),
                "daily_loss_limit_pct": str(context.config.daily_loss_limit_pct),
                "allow_counter_trend": context.config.allow_counter_trend,
            },
        },
    )


@router.post("/strategy/scoring")
async def update_scoring(
    request: Request,
    context: ContextDep,
    _session: SessionDep,
    confluence_threshold: str = Form(...),
) -> Any:
    form = await request.form()

    def error_partial(message: str) -> Any:
        return render_partial(
            request,
            "partials/strategy_scoring.html",
            {"scoring": _scoring_view(context, error=message)},
            status_code=422,
        )

    try:
        threshold = Decimal(str(confluence_threshold))
    except InvalidOperation:
        return error_partial("Порог должен быть числом от 0 до 1")
    if not Decimal("0") <= threshold <= Decimal("1"):
        return error_partial("Порог должен лежать в диапазоне 0.00 — 1.00")

    weights: dict[str, Decimal] = {}
    for module, _label in MODULE_LABELS:
        raw = form.get(f"w_{module}")
        if raw in (None, ""):
            continue
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return error_partial(f"Вес «{_label}» должен быть числом 0.00 — 1.00")
        if not Decimal("0") <= value <= Decimal("1"):
            return error_partial(f"Вес «{_label}» вне диапазона 0.00 — 1.00")
        weights[module] = value
    if not any(v > Decimal("0") for v in {**context.config.confluence_weights, **weights}.values()):
        return error_partial("Хотя бы один модуль должен иметь ненулевой вес")

    updated = await update_strategy_config(
        context,
        confluence_threshold=threshold,
        confluence_weights=weights or None,
    )
    return render_partial(
        request,
        "partials/strategy_scoring.html",
        {
            "scoring": _scoring_view(
                context,
                message=(
                    f"Сохранено: версия конфига v{updated.version}. "
                    "Применяется со следующего цикла решений."
                ),
            )
        },
    )
