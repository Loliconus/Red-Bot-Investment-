"""Одобрение подтверждённой гипотезы без автоматической правки стратегии."""

from __future__ import annotations

from uuid import UUID

from application.composition import AppContext
from core.domain.enums import HypothesisStatus

CONFIRM_PHRASE = "ОДОБРИТЬ ГИПОТЕЗУ"


async def approve_hypothesis(context: AppContext, hypothesis_id: UUID, confirmation: str) -> str:
    if confirmation != CONFIRM_PHRASE:
        raise ValueError(f"Введите точно: {CONFIRM_PHRASE}")
    hypotheses = await context.repository.list_hypotheses()
    target = next((h for h in hypotheses if h.id == hypothesis_id), None)
    if target is None:
        raise LookupError("Гипотеза не найдена")
    if target.status is not HypothesisStatus.CONFIRMED or target.is_overfitted:
        raise ValueError("Одобрить можно только подтверждённую walk-forward гипотезу")
    # В домене suggested_action — свободный текст, а не машинно-валидируемый
    # diff параметров. Не меняем конфиг автоматически, только фиксируем решение.
    target.mark_applied()
    await context.repository.save_hypothesis(target)
    return target.status.value
