"""Торговый цикл целиком: анализ → решение → исполнение → отчёт.

Исторически ``make_decision`` создавал планы, но их результат никем не
потреблялся: заявка брокеру не выставлялась никогда, а первое же исключение по
одной бумаге прерывало обход всей корзины. Этот юзкейс — единственное место,
где решение превращается в заявку:

1. каждый инструмент анализируется **изолированно** — ошибка одной бумаги не
   отменяет сканирование остальных;
2. ENTER с ненулевым сайзингом передаётся в ``execute_plan`` — тот сам
   отклоняет план при активном kill switch или нулевом размере;
3. итог каждого прохода остаётся в ``DecisionCycleReport`` и публикуется
   событием — отсюда GUI узнаёт, кого бот сканировал, кого пропустил и почему.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from application.events import DecisionCycleCompleted
from application.use_cases.execute_order import execute_plan
from application.use_cases.make_decision import make_decision
from application.use_cases.monitor_positions import refresh_regime_cache
from core.domain.enums import DecisionType, OrderStatus, Timeframe

if TYPE_CHECKING:
    from application.composition import AppContext

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentScan:
    """Итог одного прохода по одному инструменту."""

    uid: str
    ticker: str
    status: str  # enter | hold | reject | error
    detail: str
    score: str | None = None
    executed: bool = False
    order_status: str | None = None
    duration_ms: int = 0


@dataclass(slots=True)
class DecisionCycleReport:
    """Отчёт одного цикла: видимость «бот думал о каждой бумаге»."""

    started_at: datetime
    finished_at: datetime | None = None
    scans: list[InstrumentScan] = field(default_factory=list)

    @property
    def entered(self) -> int:
        return sum(1 for scan in self.scans if scan.status == DecisionType.ENTER.value)

    @property
    def executed(self) -> int:
        return sum(1 for scan in self.scans if scan.executed)

    @property
    def errors(self) -> int:
        return sum(1 for scan in self.scans if scan.status == "error")

    @property
    def duration_ms(self) -> int:
        if self.finished_at is None:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def summary(self) -> dict[str, object]:
        """Компактное представление для WS и шаблонов."""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "instruments": len(self.scans),
            "entered": self.entered,
            "executed": self.executed,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "scans": [
                {
                    "uid": scan.uid,
                    "ticker": scan.ticker,
                    "status": scan.status,
                    "detail": scan.detail,
                    "score": scan.score,
                    "executed": scan.executed,
                    "order_status": scan.order_status,
                }
                for scan in self.scans
            ],
        }


async def _refresh_portfolio(ctx: AppContext) -> None:
    """После исполнения подтягиваем баланс, если адаптер его отдаёт."""
    get_portfolio = getattr(ctx.broker, "get_portfolio", None)
    if get_portfolio is None:
        return
    try:
        ctx.portfolio = await get_portfolio()
    except Exception:  # noqa: BLE001 — портфель обновится при следующем цикле
        logger.debug("portfolio_refresh_skipped")


async def run_decision_cycle(ctx: AppContext) -> DecisionCycleReport:
    """Один проход по корзине: решение по каждой бумаге + исполнение ENTER."""
    report = DecisionCycleReport(started_at=ctx.clock.now())

    for instrument in ctx.tradable_instruments:
        started = time.monotonic()
        try:
            outcome = await make_decision(ctx, instrument)
        except Exception as exc:
            # Одна «битая» бумага не должна лишать анализа остальные:
            # именно так раньше пропадали целые инструменты без единой карточки.
            logger.exception("decision_cycle_instrument_failed", uid=instrument.uid)
            report.scans.append(
                InstrumentScan(
                    uid=instrument.uid,
                    ticker=instrument.ticker,
                    status="error",
                    detail=f"{type(exc).__name__}: анализ недоступен, см. лог",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
            continue

        regime = outcome.market_snapshot.market_regime.get(Timeframe.H1)
        if regime is not None:
            refresh_regime_cache(ctx, instrument.uid, regime)

        executed = False
        order_status: str | None = None
        if outcome.should_execute and outcome.plan is not None and outcome.sizing is not None:
            # ENTER с сайзингом: единственный в системе путь к реальной заявке.
            # execute_plan сам отклонит план при kill switch / нулевом размере.
            result = await execute_plan(ctx, outcome.plan, outcome.sizing)
            order_status = result.status.value
            executed = result.status is OrderStatus.FILLED
            if result.status is not OrderStatus.FILLED:
                logger.info(
                    "decision_cycle_order_not_filled",
                    uid=instrument.uid,
                    status=result.status.value,
                    message=result.message,
                )
            else:
                await _refresh_portfolio(ctx)

        report.scans.append(
            InstrumentScan(
                uid=instrument.uid,
                ticker=instrument.ticker,
                status=outcome.decision.value,
                detail=outcome.reason,
                score=str(outcome.decision_snapshot.confluence_score),
                executed=executed,
                order_status=order_status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )

    report.finished_at = ctx.clock.now()
    ctx.decision_scan_report = report
    await ctx.event_bus.publish(DecisionCycleCompleted(report))
    logger.info(
        "decision_cycle_finished",
        instruments=len(report.scans),
        entered=report.entered,
        executed=report.executed,
        errors=report.errors,
        duration_ms=report.duration_ms,
    )
    return report
