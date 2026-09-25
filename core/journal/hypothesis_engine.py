"""Движок гипотез самоанализа.

Жизненный цикл::

    PROPOSED (минимум ``min_sample_size`` сделок в выборке)
        → TESTING (walk-forward валидация на исторических данных)
        → CONFIRMED / REJECTED
        → APPLIED (только после ручного подтверждения пользователем в GUI)

Общепринятая защита от переобучения — walk-forward, где параметры находятся на
одном сегменте данных и проверяются на другом, ранее не использованном.
Walk-forward efficiency выше 0.5 считается приемлемым, ниже 0.3 — признак
переобучения. Ни одна гипотеза не применяется к боевым параметрам без
прохождения этой проверки и ручного одобрения.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from core.domain.enums import HypothesisStatus
from core.journal.trade_review import TradeReview

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

DEFAULT_MIN_SAMPLE_SIZE = 30
DEFAULT_WALK_FORWARD_THRESHOLD = Decimal("0.5")
#: Ниже этого значения гипотеза считается переобученной.
OVERFIT_THRESHOLD = Decimal("0.3")

#: Условие гипотезы: разбиение истории на «подпадает / не подпадает».
HypothesisCondition = Callable[[TradeReview], bool]


@dataclass(slots=True, kw_only=True)
class Hypothesis:
    """Статистическая гипотеза о систематическом слабом месте стратегии."""

    id: UUID
    text: str
    condition_description: str
    sample_size: int
    confidence: Decimal
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    suggested_action: str = ""
    walk_forward_efficiency: Decimal | None = None
    created_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    condition: HypothesisCondition | None = None

    @classmethod
    def create(
        cls,
        *,
        text: str,
        condition_description: str,
        sample_size: int,
        confidence: Decimal,
        suggested_action: str = "",
        created_at: datetime | None = None,
        condition: HypothesisCondition | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> Hypothesis:
        return cls(
            id=uuid4(),
            text=text,
            condition_description=condition_description,
            sample_size=sample_size,
            confidence=confidence,
            suggested_action=suggested_action,
            created_at=created_at,
            condition=condition,
            evidence=evidence or {},
        )

    def can_enter_testing(self, min_sample_size: int) -> bool:
        return self.status is HypothesisStatus.PROPOSED and self.sample_size >= min_sample_size

    def record_walk_forward(
        self,
        efficiency: Decimal,
        *,
        threshold: Decimal = DEFAULT_WALK_FORWARD_THRESHOLD,
    ) -> HypothesisStatus:
        """Фиксирует результат walk-forward и переводит статус.

        ``efficiency`` = OOS-метрика / IS-метрика. Выше 0.5 — приемлемо,
        ниже 0.3 — переобучение.
        """
        self.walk_forward_efficiency = efficiency
        if efficiency >= threshold:
            self.status = HypothesisStatus.CONFIRMED
        else:
            self.status = HypothesisStatus.REJECTED
        return self.status

    def mark_applied(self) -> None:
        if self.status is not HypothesisStatus.CONFIRMED:
            msg = f"Применить можно только CONFIRMED-гипотезу, текущий статус: {self.status}"
            raise ValueError(msg)
        self.status = HypothesisStatus.APPLIED

    @property
    def is_overfitted(self) -> bool:
        return (
            self.walk_forward_efficiency is not None
            and self.walk_forward_efficiency < OVERFIT_THRESHOLD
        )


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return ZERO
    return sum(values, ZERO) / Decimal(len(values))


def split_by_condition(
    reviews: Sequence[TradeReview],
    condition: HypothesisCondition,
) -> tuple[list[TradeReview], list[TradeReview]]:
    matched = [r for r in reviews if condition(r)]
    rest = [r for r in reviews if not condition(r)]
    return matched, rest


def _win_rate(rows: Sequence[TradeReview]) -> Decimal:
    if not rows:
        return ZERO
    wins = sum(1 for r in rows if r.realized_pnl > ZERO)
    return Decimal(wins) / Decimal(len(rows))


def _confidence(matched: Sequence[TradeReview], rest: Sequence[TradeReview]) -> Decimal:
    """Простая мера различимости: |Δ win rate| / max(σ-подобный разброс, ε)."""
    if not matched or not rest:
        return ZERO
    delta = abs(_win_rate(matched) - _win_rate(rest))
    scale = ONE / Decimal(min(len(matched), len(rest))).sqrt() if len(matched) > 1 else ONE
    return min(delta / max(scale, Decimal("0.05")), ONE)


def propose_efficiency_hypotheses(
    reviews: Sequence[TradeReview],
    *,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    now: datetime | None = None,
) -> list[Hypothesis]:
    """Формирует гипотезы по накопленной истории сделок.

    Гипотезы детерминированы и основаны на наблюдаемых метриках:
    1. низкая exit efficiency («отдаём движение»);
    2. преждевременные выходы (post-exit drift выше порога);
    3. пересиживание (вердикт OVERSTAYED);
    4. сделки с широким стопом (MAE больше порога) хуже остальных.

    Гипотеза формируется, только если в подвыборку попало достаточно сделок.
    """
    if len(reviews) < min_sample_size:
        return []

    now = now or datetime.now(tz=reviews[0].closed_at.tzinfo)
    proposals: list[Hypothesis] = []

    specs: tuple[tuple[str, str, HypothesisCondition, str], ...] = (
        (
            "Сделки с низкой эффективностью выхода систематически недобирают прибыль",
            "exit_efficiency < 0.4",
            lambda r: r.exit_efficiency < Decimal("0.4"),
            "Подтянуть тейк: частичная фиксация на 1R, перенос остатка в безубыток",
        ),
        (
            "Выход преждевременный: цена систематически уходит выше после закрытия",
            "post_exit_drift_pct > 0.02",
            lambda r: r.post_exit_drift_pct > Decimal("0.02"),
            "Увеличить TTL идеи и/или цель, добавить трейлинг вместо фиксированного тейка",
        ),
        (
            "Пересиживание: значимая часть прибыли отдаётся обратно",
            "verdict == OVERSTAYED",
            lambda r: r.verdict.value == "overstayed",
            "Добавить трейлинг-стоп по ATR после достижения 1R",
        ),
        (
            "Широкий стоп ухудшает результат: MAE больше медианной просадки",
            "mae > 0.02",
            lambda r: r.mae > Decimal("0.02"),
            "Ограничить максимальный риск на сделку через ATR-множитель",
        ),
    )

    for text, description, condition, action in specs:
        matched, rest = split_by_condition(reviews, condition)
        if len(matched) < min_sample_size or not rest:
            continue
        confidence = _confidence(matched, rest)
        if confidence < Decimal("0.3"):
            continue
        proposals.append(
            Hypothesis.create(
                text=text,
                condition_description=description,
                sample_size=len(matched),
                confidence=confidence,
                suggested_action=action,
                created_at=now,
                condition=condition,
                evidence={
                    "matched_win_rate": str(_win_rate(matched)),
                    "rest_win_rate": str(_win_rate(rest)),
                    "matched_mean_efficiency": str(mean([r.exit_efficiency for r in matched])),
                    "rest_mean_efficiency": str(mean([r.exit_efficiency for r in rest])),
                },
            )
        )

    return proposals


def walk_forward_split(
    reviews: Sequence[TradeReview],
    *,
    train_ratio: float = 0.6,
) -> tuple[list[TradeReview], list[TradeReview]]:
    """Разбивает историю на in-sample и out-of-sample по времени.

    Строго по порядку: OOS — хвост выборки, который при формировании гипотезы
    не использовался.
    """
    ordered = sorted(reviews, key=lambda r: r.closed_at)
    cut = int(len(ordered) * train_ratio)
    cut = max(1, min(cut, len(ordered) - 1)) if len(ordered) > 1 else len(ordered)
    return ordered[:cut], ordered[cut:]


def walk_forward_efficiency(
    reviews: Sequence[TradeReview],
    condition: HypothesisCondition,
    *,
    train_ratio: float = 0.6,
    metric: Callable[[Sequence[TradeReview]], Decimal] | None = None,
) -> Decimal | None:
    """Считает walk-forward efficiency: OOS-метрика / IS-метрика.

    Выше 0.5 — приемлемо; ниже 0.3 — признак переобучения.
    """
    metric = metric or (lambda rows: mean([r.realized_pnl for r in rows]) or ONE)
    train, test = walk_forward_split(reviews, train_ratio=train_ratio)
    matched_is, _ = split_by_condition(train, condition)
    matched_oos, _ = split_by_condition(test, condition)
    if not matched_is or not matched_oos:
        return None

    in_sample = metric(matched_is)
    out_sample = metric(matched_oos)
    if in_sample == ZERO:
        return None
    return out_sample / in_sample


def default_holding_ttl(hours: int) -> timedelta:
    return timedelta(hours=hours)
