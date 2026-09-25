"""Разбор закрытой сделки: MFE/MAE, эффективность выхода, пост-выходной дрейф.

* **MFE** (Maximum Favorable Excursion) — наибольшая нереализованная прибыль
  внутри сделки от точки входа до лучшей достигнутой точки.
* **MAE** (Maximum Adverse Excursion) — наибольшая нереализованная просадка.
* **Exit efficiency** — доля MFE, зафиксированная как реальная прибыль.
* **Post-exit drift** — сравнение цены выхода с ценой закрытия сессии и через
  1/3 дня после выхода. Формализует кейс «закрыл в 10, день закрылся в 25».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.domain.enums import TradeVerdict
from core.domain.value_objects import OHLCV

ZERO = Decimal("0")
ONE = Decimal("1")

#: Порог «значимой» просадки переданной прибыли для вердикта OVERSTAYED.
OVERSTAY_GIVEBACK_PCT = Decimal("0.5")
#: Минимальный MFE (в долях), ниже которого выход считается «осторожным».
LOW_MFE_PCT = Decimal("0.005")
#: Дрейф после выхода, начиная с которого выход считается преждевременным.
PREMATURE_DRIFT_PCT = Decimal("0.02")


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeExcursion:
    """Экстремумы движения цены внутри сделки."""

    mfe_price: Decimal  # лучшая цена для long
    mae_price: Decimal  # худшая цена для long

    def mfe_pct(self, entry: Decimal) -> Decimal:
        if entry == ZERO:
            return ZERO
        return (self.mfe_price - entry) / entry

    def mae_pct(self, entry: Decimal) -> Decimal:
        if entry == ZERO:
            return ZERO
        return (self.mae_price - entry) / entry


def compute_excursion(candles: list[OHLCV], *, from_: datetime | None = None) -> TradeExcursion:
    """Считает MFE/MAE по последовательности свечей удержания позиции.

    Для long: MFE = максимум high, MAE = минимум low.
    """
    if not candles:
        msg = "Нельзя посчитать экскурсию без свечей"
        raise ValueError(msg)

    highs = [c.high for c in candles if from_ is None or c.timestamp >= from_]
    lows = [c.low for c in candles if from_ is None or c.timestamp >= from_]
    if not highs or not lows:
        msg = "Нет свечей после起点 удержания"
        raise ValueError(msg)

    return TradeExcursion(mfe_price=max(highs), mae_price=min(lows))


def compute_exit_efficiency(entry: Decimal, exit_price: Decimal, mfe_price: Decimal) -> Decimal:
    """Доля потенциального движения, пойманная сделкой.

    ``(exit - entry) / (mfe - entry)``. Если MFE не превысил вход, возвращаем 0.
    """
    potential = mfe_price - entry
    if potential <= ZERO:
        return ZERO
    captured = exit_price - entry
    if captured <= ZERO:
        return ZERO
    efficiency = captured / potential
    return min(efficiency, ONE)


def compute_post_exit_drift(exit_price: Decimal, reference_price: Decimal) -> Decimal:
    """Насколько цена ушла дальше после выхода, в долях от цены выхода."""
    if exit_price == ZERO:
        return ZERO
    return (reference_price - exit_price) / exit_price


def classify_verdict(
    *,
    entry: Decimal,
    exit_price: Decimal,
    mfe_price: Decimal,
    post_exit_drift_pct: Decimal,
) -> TradeVerdict:
    """Классифицирует качество выхода.

    Логика:
    * убыточная сделка — ``LOSS``;
    * MFE пройден более чем на ``OVERSTAY_GIVEBACK_PCT`` обратно — ``OVERSTAYED``;
    * цена после выхода ушла сильно выше — ``PREMATURE_EXIT``;
    * MFE ничтожен, но выход не в убыток — ``CORRECT_CAUTION``;
    * иначе — ``GOOD_EXIT``.
    """
    if exit_price < entry:
        return TradeVerdict.LOSS

    mfe_gain = mfe_price - entry
    realized = exit_price - entry

    if mfe_gain > ZERO and realized < mfe_gain * (ONE - OVERSTAY_GIVEBACK_PCT):
        return TradeVerdict.OVERSTAYED

    if post_exit_drift_pct > PREMATURE_DRIFT_PCT:
        return TradeVerdict.PREMATURE_EXIT

    if mfe_gain <= entry * LOW_MFE_PCT:
        return TradeVerdict.CORRECT_CAUTION

    return TradeVerdict.GOOD_EXIT


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeReview:
    """Итоговый разбор закрытой сделки — основа для самоанализа."""

    trade_plan_id: UUID
    entry_price: Decimal
    exit_price: Decimal
    mfe: Decimal  # в долях от входа
    mae: Decimal  # в долях от входа
    exit_efficiency: Decimal
    price_at_session_close: Decimal
    price_at_t_plus_1d: Decimal | None
    price_at_t_plus_3d: Decimal | None
    post_exit_drift_pct: Decimal
    verdict: TradeVerdict
    closed_at: datetime
    holding_seconds: int = 0
    realized_pnl: Decimal = ZERO

    @classmethod
    def from_prices(
        cls,
        *,
        trade_plan_id: UUID,
        entry_price: Decimal,
        exit_price: Decimal,
        mfe_price: Decimal,
        mae_price: Decimal,
        price_at_session_close: Decimal,
        price_at_t_plus_1d: Decimal | None = None,
        price_at_t_plus_3d: Decimal | None = None,
        closed_at: datetime,
        holding_seconds: int = 0,
        quantity: int = 0,
    ) -> TradeReview:
        """Собирает разбор из «сырых» цен. Все производные считает сам."""
        mfe = _safe_pct(mfe_price, entry_price)
        mae = _safe_pct(mae_price, entry_price)
        efficiency = compute_exit_efficiency(entry_price, exit_price, mfe_price)
        drift = compute_post_exit_drift(exit_price, price_at_session_close)
        verdict = classify_verdict(
            entry=entry_price,
            exit_price=exit_price,
            mfe_price=mfe_price,
            post_exit_drift_pct=drift,
        )
        return cls(
            trade_plan_id=trade_plan_id,
            entry_price=entry_price,
            exit_price=exit_price,
            mfe=mfe,
            mae=mae,
            exit_efficiency=efficiency,
            price_at_session_close=price_at_session_close,
            price_at_t_plus_1d=price_at_t_plus_1d,
            price_at_t_plus_3d=price_at_t_plus_3d,
            post_exit_drift_pct=drift,
            verdict=verdict,
            closed_at=closed_at,
            holding_seconds=holding_seconds,
            realized_pnl=(exit_price - entry_price) * Decimal(quantity),
        )


def _safe_pct(value: Decimal, base: Decimal) -> Decimal:
    if base == ZERO:
        return ZERO
    return (value - base) / base
