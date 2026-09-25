"""Формирование человекочитаемых советов из подтверждённых гипотез.

Модуль только **советует**. Изменение боевого конфига — исключительно через
``application/use_cases/apply_hypothesis.py`` с явным флагом
``confirmed_by_user=True``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.domain.enums import HypothesisStatus
from core.journal.hypothesis_engine import Hypothesis

SEVERITY_ORDER: dict[HypothesisStatus, int] = {
    HypothesisStatus.CONFIRMED: 0,
    HypothesisStatus.TESTING: 1,
    HypothesisStatus.PROPOSED: 2,
    HypothesisStatus.APPLIED: 3,
    HypothesisStatus.REJECTED: 4,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Advice:
    """Один совет для пользователя."""

    hypothesis_id: str
    text: str
    suggested_action: str
    confidence: Decimal
    sample_size: int
    status: HypothesisStatus
    walk_forward_efficiency: Decimal | None = None

    def render(self) -> str:
        """Текст для GUI и Telegram-алерта."""
        head = f"[{self.status.value.upper()}] {self.text}"
        body = f"  совет: {self.suggested_action}" if self.suggested_action else ""
        stats = f"  выборка: {self.sample_size} сделок, уверенность: {self.confidence:.2f}" + (
            f", walk-forward: {self.walk_forward_efficiency:.2f}"
            if self.walk_forward_efficiency is not None
            else ""
        )
        return "\n".join(part for part in (head, body, stats) if part)


def build_advice(hypotheses: Sequence[Hypothesis], *, limit: int = 10) -> list[Advice]:
    """Сортирует гипотезы по значимости и превращает их в советы.

    Верх списка — подтверждённые с максимальной уверенностью. Отклонённые
    и уже применённые идут в конец и не требуют действий.
    """
    ranked = sorted(
        hypotheses,
        key=lambda h: (
            SEVERITY_ORDER.get(h.status, 9),
            -h.confidence,
            -h.sample_size,
        ),
    )
    return [
        Advice(
            hypothesis_id=str(h.id),
            text=h.text,
            suggested_action=h.suggested_action,
            confidence=h.confidence,
            sample_size=h.sample_size,
            status=h.status,
            walk_forward_efficiency=h.walk_forward_efficiency,
        )
        for h in ranked[:limit]
    ]


def has_actionable(hypotheses: Sequence[Hypothesis]) -> bool:
    """Есть ли гипотезы, требующие решения пользователя."""
    return any(h.status is HypothesisStatus.CONFIRMED for h in hypotheses)
