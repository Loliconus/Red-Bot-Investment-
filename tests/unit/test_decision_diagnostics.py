"""Read-модель «Мысли бота»: причины молчания без домыслов по сохранённым данным."""

from __future__ import annotations

from typing import Any

from application.use_cases.decision_diagnostics import (
    categorize_reason,
    reasoning_overview,
)


def test_categorize_reason_maps_known_needles() -> None:
    cases = {
        "Confluence 0.21 ниже порога 0.30": "confluence_low",
        "Бумага слабее IMOEX за 20 сессий": "below_benchmark",
        "Режим высокой волатильности — вход запрещён": "high_volatility",
        "Спред 0.4% выше допуска": "timing",
        "Сетап не подтвержден по H1": "setup",
        "Цель не проходит фильтр издержек": "costs",
        "Результат сайзинга: 0 лотов": "sizing",
        "kill switch активен": "kill_switch",
        "какая-то незнакомая причина": "other",
    }
    for reason, expected in cases.items():
        code, label = categorize_reason(reason)
        assert code == expected, (reason, code, label)
        assert label


async def test_reasoning_overview_empty_window(context: Any) -> None:
    overview = await reasoning_overview(context, hours=24)

    assert overview["window_hours"] == 24
    assert overview["totals"]["analyses"] == 0
    assert [stage["count"] for stage in overview["funnel"]] == [0, 0, 0, 0]
    assert overview["blockers"] == []
    # Инструмент из корзины есть в покрытии с честным статусом, а не домыслом.
    assert overview["coverage"][0]["ticker"] == "SBER"
    assert overview["coverage"][0]["state"] == "never"
    assert overview["coverage"][0]["scans"] == 0
    # Именно эта фраза должна читаться как «бот молчит, потому что ещё не сканировал».
    assert "цикл" in overview["silence_hint"].casefold()


async def test_reasoning_overview_groups_blockers(context: Any) -> None:
    import dataclasses
    from decimal import Decimal

    from application.use_cases.make_decision import make_decision
    from core.domain.enums import DecisionType

    # Детерминированный REJECT: при мизерном капитале сайзинг даёт ноль лотов.
    context.portfolio = dataclasses.replace(
        context.portfolio,
        total_value=Decimal("1"),
        available_cash=Decimal("1"),
    )
    outcome = await make_decision(context, context.instruments[0])
    assert outcome.decision is DecisionType.REJECT
    assert "сайзинг" in outcome.reason.casefold()

    overview = await reasoning_overview(context, hours=24)

    assert overview["totals"]["analyses"] == 1
    assert overview["totals"]["reject"] == 1
    assert overview["coverage"][0]["scans"] == 1
    assert overview["coverage"][0]["state"] == "reject"
    assert overview["blockers"]
    top = overview["blockers"][0]
    assert top["code"] == "sizing"
    assert top["count"] == 1
    assert top["example"]


async def test_reasoning_overview_disabled_state_wins(context: Any) -> None:
    context.instrument_enabled = {context.instruments[0].uid: False}
    overview = await reasoning_overview(context, hours=24)
    row = overview["coverage"][0]
    assert row["state"] == "disabled"
    assert row["state_label"] == "ОТКЛЮЧЕНА"
