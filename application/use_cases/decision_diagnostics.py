"""Диагностика решений: почему бот торгует или молчит.

Read model для экрана «Мысли бота». Отвечает на три вопроса оператора:

1. **Воронка** — сколько анализов прошло каждую стадию за окно
   (скан → сетап → риск-проверки → ENTER → исполнение);
2. **Причины молчания** — сгруппированные причины HOLD/REJECT за окно
   (без этого понять «почему 73 стратегии и ни одной сделки» невозможно);
3. **Покрытие корзины** — по каждой бумаге: сканировалась ли вообще,
   что решили в последний раз и были ли ошибки в цикле.

Никаких домыслов: всё считается только по сохранённым снапшотам и отчёту
последнего цикла ``AppContext.decision_scan_report``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from application.use_cases.gui_views import decision_view
from core.domain.enums import DecisionType

if TYPE_CHECKING:
    from application.composition import AppContext
    from core.ports.persistence import DecisionRecord

WINDOW_CHOICES_HOURS: tuple[int, ...] = (24, 72, 168)


@dataclass(frozen=True, slots=True)
class BlockerCategory:
    code: str
    label: str
    needles: tuple[str, ...]


#: Классификация свободных текстов причин из ``risk_check_reason``.
BLOCKER_CATEGORIES: tuple[BlockerCategory, ...] = (
    BlockerCategory(
        "confluence_low",
        "Confluence ниже порога",
        ("ниже порога", "ниже минимума"),
    ),
    BlockerCategory(
        "below_benchmark",
        "Бумага слабее IMOEX",
        ("слабее imoex",),
    ),
    BlockerCategory(
        "high_volatility",
        "Режим высокой волатильности",
        ("волатильности",),
    ),
    BlockerCategory(
        "timing",
        "Неподходящий момент входа",
        ("спред", "перевес продавцов", "выше vwap"),
    ),
    BlockerCategory(
        "setup",
        "Сетап не подтверждён",
        ("не подтверждён", "не подтвержден"),
    ),
    BlockerCategory(
        "costs",
        "Цель не окупает издержки",
        ("фильтр издержек",),
    ),
    BlockerCategory(
        "sizing",
        "Нулевой размер позиции",
        ("сайзинг",),
    ),
    BlockerCategory(
        "kill_switch",
        "Блокировка торговли (пауза / kill switch)",
        ("kill switch",),
    ),
)


def categorize_reason(reason: str) -> tuple[str, str]:
    """Нормализует свободный текст причины в (код, подпись) категории."""
    lowered = reason.casefold()
    for category in BLOCKER_CATEGORIES:
        if any(needle in lowered for needle in category.needles):
            return category.code, category.label
    return "other", "Прочие причины"


def _funnel(records: list[DecisionRecord], filled_plan_ids: set[str]) -> list[dict[str, Any]]:
    scans = len(records)
    # Сетап + тайминг миновали все, кто не остановился на HOLD.
    past_setup = [r for r in records if r.snapshot.decision is not DecisionType.HOLD]
    entered = [r for r in records if r.snapshot.decision is DecisionType.ENTER]
    executed = len(filled_plan_ids)
    return [
        {"stage": "Анализов бумаг", "count": scans},
        {"stage": "Прошли сетап и тайминг", "count": len(past_setup)},
        {"stage": "Прошли риск-проверки (ENTER)", "count": len(entered)},
        {"stage": "Исполнено заявок", "count": executed},
    ]


async def reasoning_overview(context: AppContext, *, hours: int = 24) -> dict[str, Any]:
    """Собирает полную модель экрана «Мысли бота» за окно ``hours`` часов."""
    hours = min(max(hours, 1), 24 * 30)
    now = context.clock.now()
    since = now - timedelta(hours=hours)

    records = await context.repository.list_decisions_since(since)
    open_plans = await context.repository.get_open_trade_plans()

    filled_plan_ids = {
        str(plan.id) for plan in open_plans if plan.status.value in {"active", "pending"}
    }

    by_decision: Counter[str] = Counter(r.snapshot.decision.value for r in records)
    blockers: Counter[str] = Counter()
    blocker_examples: dict[str, str] = {}
    for record in records:
        if record.snapshot.decision is DecisionType.ENTER:
            continue
        reason = record.snapshot.risk_check_reason or "причина не записана"
        code, label = categorize_reason(reason)
        blockers[code] += 1
        blocker_examples.setdefault(code, f"{label} — {reason[:180]}")

    latest_by_uid: dict[str, DecisionRecord] = {}
    scans_by_uid: Counter[str] = Counter()
    for record in records:
        scans_by_uid[record.instrument_uid] += 1
        latest_by_uid.setdefault(record.instrument_uid, record)

    report = context.decision_scan_report
    report_by_uid: dict[str, Any] = {scan.uid: scan for scan in report.scans} if report else {}

    coverage: list[dict[str, Any]] = []
    for instrument in context.instruments:
        if instrument.is_benchmark:
            continue
        enabled = context.instrument_enabled.get(instrument.uid, True)
        latest = latest_by_uid.get(instrument.uid)
        scan = report_by_uid.get(instrument.uid)
        if not enabled:
            state, state_label = "disabled", "ОТКЛЮЧЕНА"
        elif scan is not None and scan.status == "error":
            state, state_label = "error", "ОШИБКА ЦИКЛА"
        elif latest is None and scan is None:
            state, state_label = "never", "НЕ СКАНИРОВАЛАСЬ"
        elif latest is not None and latest.snapshot.decision is DecisionType.ENTER:
            state, state_label = "enter", "ENTER"
        elif latest is not None and latest.snapshot.decision is DecisionType.REJECT:
            state, state_label = "reject", "ОТКЛОНЕНО"
        else:
            state, state_label = "hold", "НАБЛЮДЕНИЕ"
        coverage.append(
            {
                "uid": instrument.uid,
                "ticker": instrument.ticker,
                "enabled": enabled,
                "state": state,
                "state_label": state_label,
                "scans": scans_by_uid.get(instrument.uid, 0),
                "score": str(latest.snapshot.confluence_score) if latest else None,
                "decision": latest.snapshot.decision.value if latest else None,
                "reason": (latest.snapshot.risk_check_reason or "") if latest else "",
                "thought": latest.snapshot.thought_text if latest else "",
                "last_at": latest.snapshot.created_at.isoformat() if latest else None,
                "cycle_error": scan.detail if scan is not None and scan.status == "error" else "",
            }
        )
    state_order = {"error": 0, "enter": 1, "reject": 2, "never": 3, "hold": 4, "disabled": 5}
    coverage.sort(key=lambda row: (state_order.get(row["state"], 9), row["ticker"]))

    latest_decisions = [decision_view(record, context) for record in records[:60]]

    top_blockers = [
        {
            "code": code,
            "label": _label(code),
            "count": count,
            "share": round(count / max(len(records), 1) * 100, 1),
            "example": blocker_examples.get(code, ""),
        }
        for code, count in blockers.most_common()
    ]

    return {
        "generated_at": now.isoformat(),
        "window_hours": hours,
        "window_choices": list(WINDOW_CHOICES_HOURS),
        "threshold": str(context.config.confluence_threshold),
        "totals": {
            "analyses": len(records),
            "enter": by_decision.get(DecisionType.ENTER.value, 0),
            "hold": by_decision.get(DecisionType.HOLD.value, 0),
            "reject": by_decision.get(DecisionType.REJECT.value, 0),
            "open_positions": len(filled_plan_ids),
        },
        "funnel": _funnel(records, filled_plan_ids),
        "blockers": top_blockers,
        "coverage": coverage,
        "decisions": latest_decisions,
        "scan_report": report.summary() if report else None,
        "silence_hint": _silence_hint(context, records, report, top_blockers),
    }


def _label(code: str) -> str:
    return next((c.label for c in BLOCKER_CATEGORIES if c.code == code), "Прочие причины")


def _silence_hint(
    context: AppContext,
    records: list[DecisionRecord],
    report: Any,
    blockers: list[dict[str, Any]],
) -> str:
    """Одна честная фраза о том, почему молчание — или что смотреть дальше."""
    if report is None:
        return (
            "Цикл решений ещё не завершался в этом процессе. Если бот запущен "
            "(пульт управления), первый проход выполняется сразу после старта."
        )
    if report.errors:
        tickers = ", ".join(s.ticker for s in report.scans if s.status == "error")
        return (
            f"Часть бумаг не просканирована из-за ошибок цикла: {tickers}. Смотрите покрытие ниже."
        )
    if not records:
        instruments = len(context.tradable_instruments)
        if instruments == 0:
            return "В корзине нет активных инструментов — боту нечего анализировать."
        return (
            "Записей решений за окно нет: либо окно слишком короткое, "
            "либо сохранение снапшотов недоступно — проверьте хранилище."
        )
    entered = sum(1 for r in records if r.snapshot.decision is DecisionType.ENTER)
    if entered == 0:
        top = blockers[0]["label"].lower() if blockers else "см. блокеры"
        return f"ENTER за окно не было. Главная причина отказов: {top} — полный разбор ниже."
    return "Решения ENTER есть. Их исполнение — в стадии «Исполнено заявок» воронки."
