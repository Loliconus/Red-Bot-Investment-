"""Мапперы: protobuf-модели T-Invest ↔ домен ``core``.

Это **единственное** место, где упоминаются ``Quotation``, ``MoneyValue`` и
прочие protobuf-типы. Домен о них не знает вообще.

Правила преобразования (важны для корректности расчётов):

* ``Quotation`` → ``Decimal`` через ``units + nano / 1e9``;
* деньги: ``Decimal``, а не ``float`` — ошибки округления на деньгах недопустимы;
* ``quantity`` в запросе — **лоты**, а не штуки;
* всё время — UTC, naive ``datetime`` считать ошибкой.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.domain.entities import Instrument, OrderResult, OrderState, PortfolioState, Position
from core.domain.enums import OrderSide, OrderStatus, Timeframe
from core.domain.value_objects import OHLCV, OrderbookLevel, OrderbookSnapshot

NANO = Decimal("1_000_000_000")
ZERO = Decimal("0")

#: Соответствие доменных таймфреймов и интервалов свечей из API.
TIMEFRAME_TO_API_INTERVAL: dict[Timeframe, int] = {
    Timeframe.M1: 1,  # CandleInterval.CANDLE_INTERVAL_1_MIN
    Timeframe.H1: 5,  # CANDLE_INTERVAL_HOUR
    Timeframe.D1: 8,  # CANDLE_INTERVAL_DAY
}


def quotation_to_decimal(quotation: Any) -> Decimal:
    """``Quotation`` (units + nano) → ``Decimal``."""
    if quotation is None:
        return ZERO
    units = Decimal(int(getattr(quotation, "units", 0)))
    nano = Decimal(int(getattr(quotation, "nano", 0)))
    return units + nano / NANO


def money_value_to_decimal(money: Any) -> Decimal:
    """``MoneyValue`` → ``Decimal`` с учётом ``nano``."""
    return quotation_to_decimal(money)


def decimal_to_quotation(value: Decimal) -> dict[str, int]:
    """``Decimal`` → словарь для конструктора ``Quotation``.

    Возвращает plain-данные, а не protobuf-объект: адаптер сам соберёт нужный
    тип, чтобы этот модуль оставался независимым от конкретной версии SDK.
    """
    value = Decimal(value)
    units = int(value)
    nano = int((value - units) * NANO)
    return {"units": units, "nano": nano}


def proto_timestamp_to_datetime(timestamp: Any) -> datetime:
    """``google.protobuf.Timestamp`` → tz-aware UTC ``datetime``."""
    seconds = int(getattr(timestamp, "seconds", 0))
    nanos = int(getattr(timestamp, "nanos", 0))
    return datetime.fromtimestamp(seconds + nanos / 1e9, tz=UTC)


def datetime_to_proto_timestamp(moment: datetime) -> dict[str, int]:
    """``datetime`` → словарь для ``Timestamp``. Naive datetime считаем ошибкой."""
    if moment.tzinfo is None:
        msg = "Naive datetime не допускается: все моменты времени обязаны быть в UTC"
        raise ValueError(msg)
    return {"seconds": int(moment.timestamp()), "nanos": int(moment.microsecond * 1000)}


def candle_to_domain(candle: Any, timeframe: Timeframe) -> OHLCV:
    """``HistoricCandle`` → ``OHLCV``. Цена — за одну бумагу, не за лот."""
    return OHLCV(
        open=quotation_to_decimal(candle.open),
        high=quotation_to_decimal(candle.high),
        low=quotation_to_decimal(candle.low),
        close=quotation_to_decimal(candle.close),
        volume=int(getattr(candle, "volume", 0)),
        timestamp=proto_timestamp_to_datetime(candle.time),
        timeframe=timeframe,
    )


def orderbook_to_domain(response: Any) -> OrderbookSnapshot:
    """``GetOrderBookResponse`` → ``OrderbookSnapshot``."""

    def levels(raw: Any) -> tuple[OrderbookLevel, ...]:
        return tuple(
            OrderbookLevel(
                price=quotation_to_decimal(item.price),
                quantity=int(getattr(item, "quantity", 0)),
            )
            for item in raw
        )

    return OrderbookSnapshot(
        bids=levels(response.bids),
        asks=levels(response.asks),
        captured_at=proto_timestamp_to_datetime(response.time)
        if hasattr(response, "time")
        else datetime.now(tz=UTC),
    )


def instrument_to_domain(instrument: Any, *, is_benchmark: bool = False) -> Instrument:
    """``Instrument`` из API → доменная сущность."""
    return Instrument(
        uid=str(instrument.uid),
        ticker=str(instrument.ticker),
        class_code=str(getattr(instrument, "class_code", "TQBR")),
        lot_size=int(instrument.lot),
        is_benchmark=is_benchmark,
        currency=str(getattr(instrument, "currency", "rub")).upper(),
    )


ORDER_STATUS_MAP: dict[int, OrderStatus] = {
    1: OrderStatus.FILLED,  # EXECUTION_REPORT_STATUS_FILL
    2: OrderStatus.PARTIALLY_FILLED,  # EXECUTION_REPORT_STATUS_PARTIALLYFILL
    3: OrderStatus.CANCELLED,  # EXECUTION_REPORT_STATUS_CANCELLED
    4: OrderStatus.ACCEPTED,  # EXECUTION_REPORT_STATUS_NEW
    5: OrderStatus.REJECTED,  # EXECUTION_REPORT_STATUS_REJECTED
}


def order_status_to_domain(status: Any) -> OrderStatus:
    """Числовой статус исполнения → доменный enum."""
    value = int(status) if not isinstance(status, int) else status
    return ORDER_STATUS_MAP.get(value, OrderStatus.UNKNOWN)


def post_order_response_to_domain(
    response: Any,
    *,
    client_order_id: str,
) -> OrderResult:
    """``PostOrderResponse`` → ``OrderResult``.

    Важно: ``response.order_id`` — это **биржевой** идентификатор, он не равен
    отправленному нами ключу идемпотентности.
    """
    return OrderResult(
        order_id=str(getattr(response, "order_id", "")),
        client_order_id=client_order_id,
        status=OrderStatus.ACCEPTED,
        filled_lots=int(getattr(response, "lots_executed", 0) or 0),
        filled_price=(
            money_value_to_decimal(response.executed_order_price)
            if hasattr(response, "executed_order_price")
            else None
        ),
        message=getattr(response, "message", "") or "",
    )


def order_state_to_domain(state: Any) -> OrderState:
    """``OrderState`` → доменная структура."""
    return OrderState(
        order_id=str(getattr(state, "order_id", "")),
        status=order_status_to_domain(getattr(state, "execution_report_status", 0)),
        filled_lots=int(getattr(state, "lots_executed", 0) or 0),
        message=getattr(state, "message", "") or "",
    )


def position_to_domain(raw: Any, instrument: Instrument, *, plan_id: Any) -> Position:
    """Позиция из портфеля → доменная сущность. Количество в **штуках**."""
    quantity_dec = quotation_to_decimal(getattr(raw, "quantity", None))
    units = int(quantity_dec)
    average = (
        money_value_to_decimal(raw.average_position_price)
        if hasattr(raw, "average_position_price")
        else ZERO
    )
    return Position(
        instrument=instrument,
        quantity=units,
        average_entry=average,
        opened_at=datetime.now(tz=UTC),
        linked_plan_id=plan_id,
    )


def lots_to_units(lots: int, lot_size: int) -> int:
    """Лоты → штуки."""
    return lots * lot_size


def units_to_lots(units: int, lot_size: int) -> int:
    """Штуки → лоты (округление вниз)."""
    return units // lot_size


def order_direction(side: OrderSide) -> int:
    """Доменный ``OrderSide`` → числовой тип направления в API (1 = BUY)."""
    if side is OrderSide.BUY:
        return 1
    msg = f"Неподдерживаемое направление: {side}"
    raise ValueError(msg)


def portfolio_response_to_domain(response: Any, *, account_id: str) -> PortfolioState:
    """``PortfolioResponse`` → ``PortfolioState``."""
    total_val = money_value_to_decimal(getattr(response, "total_amount_portfolio", None))
    avail_cash = money_value_to_decimal(getattr(response, "total_amount_currencies", None))
    pos_val = money_value_to_decimal(getattr(response, "total_amount_shares", None))
    if total_val == ZERO and avail_cash > ZERO:
        total_val = avail_cash + pos_val
    return PortfolioState(
        account_id=account_id,
        total_value=total_val,
        available_cash=avail_cash,
        positions_value=pos_val,
        updated_at=datetime.now(tz=UTC),
    )
