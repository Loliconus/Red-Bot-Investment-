"""Тайминг входа.

Отдельный этап после подтверждения сетапа: идея может быть верной, а момент —
плохим (широкий спред, цена уперлась в сопротивление стакана, откуп на
пиковых объёмах). Здесь решается, входить сейчас или подождать.

Сигналы (все из 1m-данных и стакана):
* цена ниже VWAP при восходящем наклоне OBV — хороший откат;
* широкий спред — ждём, рыночный вход съест прибыль;
* перевес заявок на продажу рядом с ценой — ждём.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.analysis.orderbook_analysis import WIDE_SPREAD_PCT
from core.domain.entities import StrategyConfig
from core.domain.enums import Timeframe
from core.journal.snapshots import MarketSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")

#: Скор confluence, ниже которого вход откладывается до подтверждения.
MIN_SCORE_TO_ENTER = Decimal("0.3")
#: Дисбаланс стакана, при котором вход откладывается.
WAIT_IMBALANCE = Decimal("-0.3")
#: Отклонение от VWAP, при котором цена считается «горячей» (перекупленной).
HOT_DEVIATION = Decimal("0.01")


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryTiming:
    """Решение о моменте входа."""

    decision: str  # "enter_now" | "wait" | "skip"
    confidence: Decimal
    reason: str

    @property
    def should_enter(self) -> bool:
        return self.decision == "enter_now"

    @property
    def should_wait(self) -> bool:
        return self.decision == "wait"


def evaluate_entry_timing(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    *,
    confluence_score: Decimal,
) -> EntryTiming:
    """Оценивает момент входа."""
    if confluence_score < MIN_SCORE_TO_ENTER:
        return EntryTiming(
            decision="skip",
            confidence=ZERO,
            reason=f"confluence {confluence_score:.3f} ниже минимума для входа",
        )

    raw = snapshot.indicators.get(Timeframe.M1, {})
    spread_pct = raw.get("spread_pct")
    imbalance = raw.get("imbalance")
    deviation = raw.get("deviation")
    obv_slope = raw.get("obv_slope")

    blockers: list[str] = []

    if spread_pct is not None and spread_pct > WIDE_SPREAD_PCT:
        blockers.append(f"широкий спред {spread_pct:.4%}")
    if imbalance is not None and imbalance <= WAIT_IMBALANCE:
        blockers.append(f"перевес продавцов в стакане ({imbalance:.2f})")
    if deviation is not None and deviation > HOT_DEVIATION:
        blockers.append("цена существенно выше VWAP — вход по плохой цене")

    if blockers:
        return EntryTiming(
            decision="wait",
            confidence=Decimal("0.3"),
            reason="; ".join(blockers),
        )

    confidence = Decimal("0.5")
    reasons: list[str] = []

    if deviation is not None and deviation <= ZERO:
        confidence += Decimal("0.2")
        reasons.append("цена на VWAP или ниже — откат")
    if obv_slope is not None and obv_slope > ZERO:
        confidence += Decimal("0.2")
        reasons.append("OBV растёт — приток объёма")
    if imbalance is not None and imbalance > ZERO:
        confidence += Decimal("0.1")
        reasons.append(f"перевес покупателей ({imbalance:.2f})")

    return EntryTiming(
        decision="enter_now",
        confidence=min(confidence, ONE),
        reason="; ".join(reasons) or "препятствий для входа нет",
    )
