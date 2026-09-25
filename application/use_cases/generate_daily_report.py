"""Ежедневный отчёт и запуск самоанализа.

Отчёт — не «скрасили красиво», а рабочий инструмент: сводка по закрытым сделкам
плюс список гипотез, которые система накопила и предлагает проверить.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from core.journal.advisory import build_advice
from core.journal.hypothesis_engine import (
    Hypothesis,
    propose_efficiency_hypotheses,
    walk_forward_efficiency,
)
from core.journal.trade_review import TradeReview

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)

ZERO = Decimal("0")
HUNDRED = Decimal("100")


async def generate_daily_report(ctx: AppContext) -> dict[str, object]:
    """Собирает отчёт за сутки и формирует новые гипотезы."""
    now = ctx.clock.now()
    since = now - timedelta(days=1)

    reviews = await ctx.repository.get_trade_history(None, since=since)

    total_pnl = sum((r.realized_pnl for r in reviews), ZERO)
    wins = [r for r in reviews if r.realized_pnl > ZERO]
    win_rate = (Decimal(len(wins)) / Decimal(len(reviews)) * HUNDRED) if reviews else ZERO
    efficiency = (
        (sum((r.exit_efficiency for r in reviews), ZERO) / Decimal(len(reviews)))
        if reviews
        else ZERO
    )

    hypotheses = await run_self_analysis(ctx, reviews=reviews)
    advice = build_advice(hypotheses)

    report = {
        "date": now,
        "trades_closed": len(reviews),
        "win_rate_pct": win_rate,
        "total_pnl": total_pnl,
        "best_trade": max((r.realized_pnl for r in reviews), default=ZERO),
        "worst_trade": min((r.realized_pnl for r in reviews), default=ZERO),
        "avg_exit_efficiency": efficiency,
        "advice": [a.render() for a in advice],
        "new_hypotheses": len(hypotheses),
    }

    logger.info(
        "daily_report_generated",
        trades=len(reviews),
        win_rate=str(win_rate),
        pnl=str(total_pnl),
    )

    if ctx.notifier is not None and advice:
        await ctx.notifier.send("\n".join(a.render() for a in advice[:3]))

    return report


async def run_self_analysis(
    ctx: AppContext,
    *,
    reviews: list[TradeReview] | None = None,
) -> list[Hypothesis]:
    """Формирует гипотезы по истории сделок и сохраняет их.

    Гипотеза проходит walk-forward проверку сразу: если эффективность ниже
    порога, она помечается отклонённой и в советы не попадает.
    """
    history = reviews
    if history is None:
        history = await ctx.repository.get_trade_history(
            None, since=ctx.clock.now() - timedelta(days=180)
        )

    min_sample = ctx.settings.risk_defaults.hypothesis_min_sample_size
    threshold = Decimal(str(ctx.settings.risk_defaults.walk_forward_confirmation_threshold))

    proposals = propose_efficiency_hypotheses(history, min_sample_size=min_sample)

    for hypothesis in proposals:
        condition = hypothesis.condition or (lambda review: False)
        efficiency = walk_forward_efficiency(history, condition)
        hypothesis.record_walk_forward(
            efficiency if efficiency is not None else ZERO, threshold=threshold
        )
        await ctx.repository.save_hypothesis(hypothesis)

    logger.info("self_analysis_completed", hypotheses=len(proposals))
    return list(proposals)
