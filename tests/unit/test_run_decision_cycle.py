"""Цикл решений: изоляция ошибок по бумагам, исполнение ENTER, отчёт.

Регрессии, которым здесь не даём вернуться:
1. исключение по одной бумаге прокатывалось вверх и лишало анализа всю
   корзину — на дашборде «у одной акции движение, а карточек вообще нет»;
2. ENTER-решения никогда не доходили до брокера, потому что путь
   ``should_execute → execute_plan`` просто отсутствовал.
"""

from __future__ import annotations

from typing import Any

import pytest

from application.events import DecisionCycleCompleted
from application.use_cases.run_decision_cycle import (
    DecisionCycleReport,
    run_decision_cycle,
)
from core.domain.enums import OrderStatus
from tests.fakes import make_instrument


async def test_cycle_executes_enter_and_publishes_report(context: Any) -> None:
    completed: list[DecisionCycleCompleted] = []
    context.event_bus.subscribe(DecisionCycleCompleted, completed.append)

    report = await run_decision_cycle(context)

    assert report.errors == 0
    assert report.entered == 1, [scan.status for scan in report.scans]
    assert report.executed == 1
    scan = report.scans[0]
    assert scan.order_status == OrderStatus.FILLED.value
    assert scan.executed
    # Брокер действительно получил заявку — это и есть «бот торгует».
    assert context.broker.placed, "ENTER должен доходить до OrderExecutionPort"
    # Отчёт доступен read-моделям и опубликован для живого GUI.
    assert context.decision_scan_report is report
    assert isinstance(context.decision_scan_report, DecisionCycleReport)
    assert completed and completed[0].report is report
    assert report.summary()["instruments"] == 1


async def test_cycle_isolates_instrument_errors(
    context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = make_instrument(uid="uid-broken", ticker="BRKN", lot_size=5)
    context.instruments = [broken, *context.instruments]

    from application.use_cases import run_decision_cycle as cycle_module

    real_make_decision = cycle_module.make_decision

    async def flaky(ctx: Any, instrument: Any) -> Any:
        if instrument.uid == "uid-broken":
            raise RuntimeError("рыночные данные испорчены")
        return await real_make_decision(ctx, instrument)

    monkeypatch.setattr(cycle_module, "make_decision", flaky)

    report = await run_decision_cycle(context)

    assert len(report.scans) == 2
    error_scan = next(scan for scan in report.scans if scan.uid == "uid-broken")
    assert error_scan.status == "error"
    assert not error_scan.executed
    # Анализ дошёл и до исправной бумаги, несмотря на ошибку соседней.
    healthy = next(scan for scan in report.scans if scan.uid != "uid-broken")
    assert healthy.status == "enter"
    assert healthy.executed
    assert report.errors == 1


async def test_cycle_without_instruments_is_empty_report(context: Any) -> None:
    context.instruments = []
    report = await run_decision_cycle(context)
    assert report.scans == []
    assert report.entered == 0
    assert report.executed == 0
