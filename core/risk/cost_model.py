"""Модель издержек и фильтр «стоит ли вообще входить».

Ключевое правило: **цель сделки обязана превышать совокупные издержки входа и
выхода как минимум в ``min_viable_target_multiplier`` раз**. По умолчанию ×2.
Без этого фильтра стратегия стабильно отдаёт брокеру часть ожидаемой прибыли на
каждой сделке, а на горизонте множества сделок это убыточно.

Составляющие издержек:
* **комиссия брокера** — round trip: платится и на входе, и на выходе;
* **спред** — покупка по ask, продажа по bid;
* **проскальзывание** — оценочная величина для рыночного ордера.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Self

from core.domain.value_objects import Percentage

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
HUNDRED = Decimal("100")

#: Оценочное проскальзывание рыночного ордера в долях от цены.
DEFAULT_SLIPPAGE_PCT = Decimal("0.0005")


@dataclass(frozen=True, slots=True, kw_only=True)
class CostEstimate:
    """Оценка совокупных издержек round-trip."""

    commission_pct: Percentage
    spread_pct: Percentage
    slippage_pct: Percentage
    total_pct: Percentage
    commission_amount: Decimal
    total_amount: Decimal

    @classmethod
    def from_pct(
        cls,
        *,
        commission_pct: Decimal,
        spread_pct: Decimal,
        slippage_pct: Decimal,
        notional: Decimal,
    ) -> Self:
        total = commission_pct + spread_pct + slippage_pct
        return cls(
            commission_pct=Percentage(value=commission_pct),
            spread_pct=Percentage(value=spread_pct),
            slippage_pct=Percentage(value=slippage_pct),
            total_pct=Percentage(value=total),
            commission_amount=notional * commission_pct,
            total_amount=notional * total,
        )


def round_trip_commission_pct(commission_rate: Decimal) -> Decimal:
    """Комиссия за полный цикл: вход + выход."""
    return commission_rate * TWO


def estimate_costs(
    *,
    notional: Decimal,
    commission_rate: Decimal,
    spread_pct: Decimal = ZERO,
    slippage_pct: Decimal = DEFAULT_SLIPPAGE_PCT,
) -> CostEstimate:
    """Считает совокупные издержки сделки в процентах и в деньгах."""
    if notional < ZERO:
        msg = f"notional не может быть отрицательным: {notional}"
        raise ValueError(msg)

    return CostEstimate.from_pct(
        commission_pct=round_trip_commission_pct(commission_rate),
        spread_pct=spread_pct,
        slippage_pct=slippage_pct,
        notional=notional,
    )


def min_viable_target_pct(
    costs_pct: Decimal,
    *,
    multiplier: Decimal = TWO,
) -> Decimal:
    """Минимально осмысленная цель: издержки × множитель."""
    return costs_pct * multiplier


def is_target_viable(
    expected_return_pct: Decimal,
    costs_pct: Decimal,
    *,
    multiplier: Decimal = TWO,
) -> bool:
    """Проходит ли ожидаемая доходность фильтр издержек."""
    return expected_return_pct >= min_viable_target_pct(costs_pct, multiplier=multiplier)


def viability_margin(
    expected_return_pct: Decimal,
    costs_pct: Decimal,
) -> Decimal:
    """Во сколько раз цель больше издержек. 0 при нулевых издержках — «бесплатно»."""
    if costs_pct == ZERO:
        return ZERO
    return expected_return_pct / costs_pct


@dataclass(frozen=True, slots=True, kw_only=True)
class CostFilterResult:
    """Результат фильтра издержек с человекочитаемой причиной."""

    passed: bool
    expected_return_pct: Decimal
    costs_pct: Decimal
    required_pct: Decimal
    actual_multiplier: Decimal
    reason: str

    @classmethod
    def evaluate(
        cls,
        *,
        expected_return_pct: Decimal,
        costs_pct: Decimal,
        multiplier: Decimal = TWO,
    ) -> Self:
        required = min_viable_target_pct(costs_pct, multiplier=multiplier)
        actual = viability_margin(expected_return_pct, costs_pct)
        passed = expected_return_pct >= required
        reason = (
            f"цель {expected_return_pct * HUNDRED:.3f}% "
            f"{'≥' if passed else '<'} "
            f"{multiplier}× издержек ({costs_pct * HUNDRED:.3f}% × {multiplier} "
            f"= {required * HUNDRED:.3f}%)"
        )
        return cls(
            passed=passed,
            expected_return_pct=expected_return_pct,
            costs_pct=costs_pct,
            required_pct=required,
            actual_multiplier=actual,
            reason=reason,
        )
