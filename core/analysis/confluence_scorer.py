"""Confluence-скор: взвешенное голосование модулей анализа.

Каждый модуль отдаёт нормированный вклад в диапазоне ``[-1, 1]``; итоговый
скор — взвешенная сумма, нормированная на сумму весов, тоже в ``[-1, 1]``.

Веса **не хардкодятся**: они приходят из ``StrategyConfig.confluence_weights``
и правятся из GUI. Это принципиально — иначе подгонка весов превращается в
правку исходников и переобучение становится неконтролируемым.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from core.domain.entities import ReasoningStep, TradeThesis
from core.domain.enums import Timeframe, Trend

ZERO = Decimal("0")
ONE = Decimal("1")
MINUS_ONE = Decimal("-1")

#: Порог, ниже которого вход не рассматривается (переопределяется конфигом).
DEFAULT_THRESHOLD = Decimal("0.3")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfluenceFactor:
    """Вклад одного модуля анализа."""

    module: str
    signal: str
    score: Decimal  # нормированный вклад в [-1, 1]
    weight: Decimal
    raw_value: Decimal | None = None
    comment: str = ""

    def __post_init__(self) -> None:
        if not (MINUS_ONE <= self.score <= ONE):
            msg = f"score обязан лежать в [-1, 1], получен {self.score}"
            raise ValueError(msg)

    def to_reasoning_step(self) -> ReasoningStep:
        return ReasoningStep(
            module=self.module,
            signal=self.signal,
            weight=self.score * self.weight,
            raw_value=self.raw_value,
            comment=self.comment,
        )


def clamp_unit(value: Decimal) -> Decimal:
    return max(MINUS_ONE, min(ONE, value))


def score_confluence(
    factors: Sequence[ConfluenceFactor],
    *,
    weights: Mapping[str, Decimal] | None = None,
) -> Decimal:
    """Итоговый confluence-скор.

    Если переданы внешние ``weights``, они перекрывают веса факторов — это
    позволяет менять настройки из GUI без пересборки пайплайна.
    """
    if not factors:
        return ZERO

    weighted = ZERO
    total = ZERO
    for factor in factors:
        weight = (weights or {}).get(factor.module, factor.weight)
        weighted += factor.score * weight
        total += weight

    if total == ZERO:
        return ZERO
    return clamp_unit(weighted / total)


def build_thesis(
    factors: Sequence[ConfluenceFactor],
    *,
    timeframe_bias: Mapping[Timeframe, Trend],
    confluence_score: Decimal,
    summary: str = "",
) -> TradeThesis:
    """Собирает ``TradeThesis`` с полной цепочкой обоснования."""
    chain = tuple(factor.to_reasoning_step() for factor in factors)
    return TradeThesis(
        reasoning_chain=chain,
        confluence_score=confluence_score,
        timeframe_bias=dict(timeframe_bias),
        summary=summary,
    )


def is_actionable(score: Decimal, threshold: Decimal = DEFAULT_THRESHOLD) -> bool:
    """Проходит ли скор порог входа."""
    return score >= threshold


def dominant_negative(factors: Sequence[ConfluenceFactor]) -> ConfluenceFactor | None:
    """Самый сильный отрицательный фактор — для объяснения отказа в GUI."""
    negatives = [f for f in factors if f.score < ZERO]
    if not negatives:
        return None
    return min(negatives, key=lambda f: f.score * f.weight)
