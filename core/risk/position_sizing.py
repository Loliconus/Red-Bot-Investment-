"""Позиционный сайзинг: размер позиции от фиксированного риска.

Классическая формула::

    Size = (Капитал × Риск%) / |Entry − Stop|

Размер считается **в единицах инструмента**, затем приводится к лотам с
округлением вниз — лучше открыть меньше, чем превысить риск-бюджет. Сверху
работает ограничение по ноционалу позиции.

Шорты и маржинальная торговля исключены из домена полностью: функция никогда
не возвращает отрицательный размер и не учитывает плечо.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from core.domain.entities import Instrument

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True, kw_only=True)
class SizingResult:
    """Результат расчёта размера позиции."""

    lots: int
    units: int
    notional: Decimal
    risk_amount: Decimal
    risk_pct: Decimal
    capped_by_notional: bool
    reason: str

    @property
    def is_empty(self) -> bool:
        return self.lots <= 0


def risk_per_unit(entry_price: Decimal, stop_price: Decimal) -> Decimal:
    """Риск на одну единицу инструмента."""
    distance = entry_price - stop_price
    if distance <= ZERO:
        msg = f"Стоп ({stop_price}) обязан быть ниже входа ({entry_price}) для long-позиции"
        raise ValueError(msg)
    return distance


def calculate_position_size(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    instrument: Instrument,
    max_position_notional: Decimal | None = None,
    current_notional: Decimal = ZERO,
) -> SizingResult:
    """Рассчитывает размер позиции в лотах.

    ``risk_pct`` — доля от единицы (0.01 == 1% капитала).
    ``max_position_notional`` — жёсткий потолок на позицию в деньгах.
    ``current_notional`` — уже занятый объём по инструменту (докупка запрещена
    без явного разрешения, поэтому обычно равен стоимости открытой позиции).
    """
    if equity <= ZERO:
        msg = f"Капитал обязан быть положительным, получен {equity}"
        raise ValueError(msg)
    if not (ZERO < risk_pct <= ONE):
        msg = f"risk_pct обязан лежать в (0, 1], получен {risk_pct}"
        raise ValueError(msg)

    budget = equity * risk_pct
    distance = risk_per_unit(entry_price, stop_price)

    raw_units = (budget / distance).quantize(ONE, rounding=ROUND_DOWN)
    units = int(raw_units)

    lots = units // instrument.lot_size
    units = lots * instrument.lot_size

    headroom = (
        max_position_notional - current_notional if max_position_notional is not None else None
    )

    capped = False
    if headroom is not None and headroom <= ZERO:
        return SizingResult(
            lots=0,
            units=0,
            notional=ZERO,
            risk_amount=ZERO,
            risk_pct=risk_pct,
            capped_by_notional=True,
            reason="лимит ноционала по инструменту уже исчерпан",
        )

    if headroom is not None:
        notional_per_unit = entry_price * ONE
        affordable_units = int((headroom / notional_per_unit).quantize(ONE, rounding=ROUND_DOWN))
        affordable_lots = affordable_units // instrument.lot_size
        if affordable_lots < lots:
            lots = affordable_lots
            units = lots * instrument.lot_size
            capped = True

    notional = entry_price * Decimal(units)
    risk_amount = distance * Decimal(units)

    if lots <= 0:
        reason = "риск-бюджет не покрывает даже один лот"
        if capped:
            reason = "лимит ноционала не позволяет купить ни одного лота"
    else:
        reason = f"риск {risk_amount} ({risk_pct * 100:.2f}% капитала)"
        if capped:
            reason += ", ограничен лимитом ноционала"

    return SizingResult(
        lots=lots,
        units=units,
        notional=notional,
        risk_amount=risk_amount,
        risk_pct=risk_pct,
        capped_by_notional=capped,
        reason=reason,
    )


def size_from_budget(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
) -> Decimal:
    """«Сырой» размер в единицах бюджета — для отчётов и GUI."""
    return (equity * risk_pct) / risk_per_unit(entry_price, stop_price)
