"""Инвалидация тезиса — выход не по цене.

Отличие от hard stop принципиальное:

* **hard stop** — цена дошла до уровня, значит рынок пошёл против нас;
* **инвалидация тезиса** — цена ещё может быть выше стопа, но причины держать
  позицию больше нет: пробит значимый уровень, сломана структура тренда,
  объёмы ушли, корреляция с рынком сменилась.

``InvalidationRule.check`` получает актуальный ``MarketSnapshot`` и возвращает
``True``, если тезис мёртв.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from core.domain.entities import InvalidationRule, ReasoningStep
from core.domain.enums import Timeframe, Trend
from core.journal.snapshots import MarketSnapshot

ZERO = Decimal("0")
ONE = Decimal("1")

#: Порог падения confluence-скора, после которого тезис считается разрушенным.
CONFLUENCE_DROP_PCT = Decimal("0.5")
#: Порог относительной силы: бумага стала отставать от IMOEX.
RELATIVE_STRENGTH_FLOOR = Decimal("-0.01")
#: Порог корреляции, выше которого движение объясняется рынком, а не идеей.
CORRELATION_CEILING = Decimal("0.9")


def combine_rules(
    rules: Sequence[InvalidationRule],
    *,
    description: str = "составное правило инвалидации",
    code: str = "combined",
) -> InvalidationRule:
    """Собирает составное правило: срабатывает, если сработало любое из них."""

    def check(snapshot: MarketSnapshot) -> bool:
        return any(rule.check(snapshot) for rule in rules)

    return InvalidationRule(description=description, check=check, code=code)


def trend_break_rule(timeframe: Timeframe) -> InvalidationRule:
    """Слом тренда: bias нужного таймфрейма перестал быть восходящим."""

    def check(snapshot: MarketSnapshot, tf: Timeframe = timeframe) -> bool:
        regime = snapshot.market_regime.get(tf)
        return regime is not None and regime.value != Trend.UP.value and regime.value != "trending"

    return InvalidationRule(
        description=f"слом структуры тренда на {timeframe.value}",
        check=check,
        code=f"trend_break_{timeframe.value}",
    )


def confluence_score_rule(
    score_provider: Callable[[MarketSnapshot], Decimal],
    *,
    threshold: Decimal,
) -> InvalidationRule:
    """Инвалидация по актуальному confluence-скору из внешнего источника."""

    def check(snapshot: MarketSnapshot) -> bool:
        return score_provider(snapshot) < threshold

    return InvalidationRule(
        description=f"confluence-скор ниже порога {threshold:.3f}",
        check=check,
        code="confluence_score_below_threshold",
    )


def relative_strength_rule(*, floor: Decimal = RELATIVE_STRENGTH_FLOOR) -> InvalidationRule:
    """Бумага перестала быть сильнее IMOEX."""

    def check(snapshot: MarketSnapshot) -> bool:
        rs = snapshot.indicators.get(Timeframe.D1, {}).get("relative_strength")
        return rs is not None and rs < floor

    return InvalidationRule(
        description=f"относительная сила против IMOEX ниже {floor}",
        check=check,
        code="relative_strength_lost",
    )


def market_driven_rule(*, ceiling: Decimal = CORRELATION_CEILING) -> InvalidationRule:
    """Движение бумаги полностью объясняется рынком (бета без альфы)."""

    def check(snapshot: MarketSnapshot) -> bool:
        corr = snapshot.indicators.get(Timeframe.D1, {}).get("correlation")
        rs = snapshot.indicators.get(Timeframe.D1, {}).get("relative_strength")
        if corr is None or rs is None:
            return False
        return corr >= ceiling and rs <= ZERO

    return InvalidationRule(
        description="движение полностью объясняется рынком: высокая корреляция без альфы",
        check=check,
        code="market_driven_only",
    )


def build_default_rules(
    *,
    entry_score: Decimal,
    score_provider: Callable[[MarketSnapshot], Decimal],
    timeframes: Sequence[Timeframe] = (Timeframe.H1,),
) -> InvalidationRule:
    """Набор правил инвалидации по умолчанию."""
    rules = [
        confluence_score_rule(
            score_provider,
            threshold=entry_score * (ONE - CONFLUENCE_DROP_PCT),
        ),
        relative_strength_rule(),
        market_driven_rule(),
        *(trend_break_rule(tf) for tf in timeframes),
    ]
    return combine_rules(rules)


def find_invalidated(
    rules: Sequence[InvalidationRule], snapshot: MarketSnapshot
) -> InvalidationRule | None:
    """Первое сработавшее правило или None."""
    for rule in rules:
        if rule.check(snapshot):
            return rule
    return None


class NoOpInvalidation:
    """Заглушка: тезис всегда жив. Для бэктеста режима «только стоп и тейк»."""

    __slots__ = ()

    def as_rule(self) -> InvalidationRule:
        return InvalidationRule(
            description="инвалидация отключена",
            check=lambda snapshot: False,
            code="noop",
        )


def explain(rules: Sequence[InvalidationRule]) -> list[ReasoningStep]:
    """Человекочитаемое описание правил для GUI."""
    return [
        ReasoningStep(
            module="thesis_invalidation",
            signal=rule.code,
            weight=ZERO,
            comment=rule.description,
        )
        for rule in rules
    ]
