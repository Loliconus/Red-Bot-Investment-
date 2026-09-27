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

from core.domain.catalog import InstrumentCatalogEntry
from core.domain.entities import Instrument, OrderResult, OrderState, PortfolioState, Position
from core.domain.enums import OrderSide, OrderStatus, Timeframe
from core.domain.value_objects import OHLCV, OrderbookLevel, OrderbookSnapshot

ZERO = Decimal("0")

#: Имена enum значений в SDK. Используем символы, а не числовые значения:
#: номера enum менялись между версиями proto, а 5 и 8 — это не час/день.
TIMEFRAME_TO_API_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "CANDLE_INTERVAL_1_MIN",
    Timeframe.H1: "CANDLE_INTERVAL_HOUR",
    Timeframe.D1: "CANDLE_INTERVAL_DAY",
}
TIMEFRAME_TO_SUBSCRIPTION_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "SUBSCRIPTION_INTERVAL_ONE_MINUTE",
    Timeframe.H1: "SUBSCRIPTION_INTERVAL_ONE_HOUR",
    Timeframe.D1: "SUBSCRIPTION_INTERVAL_ONE_DAY",
}
TIMEFRAME_TO_INDICATOR_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "INDICATOR_INTERVAL_ONE_MINUTE",
    Timeframe.H1: "INDICATOR_INTERVAL_ONE_HOUR",
    Timeframe.D1: "INDICATOR_INTERVAL_ONE_DAY",
}


def quotation_to_decimal(quotation: Any) -> Decimal:
    """Конвертирует SDK Quotation штатной функцией без float."""
    if quotation is None:
        return ZERO
    from t_tech.invest.utils import quotation_to_decimal as sdk_quotation_to_decimal

    return sdk_quotation_to_decimal(quotation)


def money_value_to_decimal(money: Any) -> Decimal:
    """Конвертирует SDK MoneyValue штатной функцией без float."""
    if money is None:
        return ZERO
    from t_tech.invest.utils import money_to_decimal

    return money_to_decimal(money)


def decimal_to_quotation(value: Decimal) -> Any:
    """Конвертирует Decimal в SDK Quotation штатной функцией."""
    from t_tech.invest.utils import decimal_to_quotation as sdk_decimal_to_quotation

    return sdk_decimal_to_quotation(value)


def proto_timestamp_to_datetime(timestamp: Any) -> datetime:
    """SDK ``datetime`` или ``google.protobuf.Timestamp`` → tz-aware UTC ``datetime``."""
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            msg = "SDK вернул naive datetime вместо UTC timestamp"
            raise ValueError(msg)
        return timestamp.astimezone(UTC)
    seconds = int(getattr(timestamp, "seconds", 0))
    nanos = int(getattr(timestamp, "nanos", 0))
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)


def datetime_to_proto_timestamp(moment: datetime) -> datetime:
    """SDK request-модели принимают ``datetime`` напрямую, не dict/protobuf."""
    if moment.tzinfo is None:
        msg = "Naive datetime не допускается: все моменты времени обязаны быть в UTC"
        raise ValueError(msg)
    return moment.astimezone(UTC)


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

    timestamp = getattr(response, "orderbook_ts", None) or getattr(response, "time", None)
    return OrderbookSnapshot(
        bids=levels(response.bids),
        asks=levels(response.asks),
        captured_at=proto_timestamp_to_datetime(timestamp)
        if timestamp is not None
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


# ------------------------------------------------------------------ каталог
def _catalog_text(raw: Any, *names: str) -> str:
    """Первое непустое строковое поле среди перечисленных (поля API опциональны)."""
    for name in names:
        value = getattr(raw, name, None)
        if value:
            return str(value)
    return ""


def _catalog_flag(raw: Any, name: str) -> bool:
    return bool(getattr(raw, name, False))


def _catalog_increment(raw: Any) -> Decimal | None:
    """Шаг цены в Decimal через штатную конвертацию Quotation."""
    value = getattr(raw, "min_price_increment", None)
    if value is None:
        return None
    return quotation_to_decimal(value)


def _catalog_lot(raw: Any) -> int:
    """Размер лота из API; у части инструментов поле пустое — берём минимум 1."""
    try:
        lot = int(getattr(raw, "lot", 0) or 0)
    except (TypeError, ValueError):
        lot = 0
    return max(lot, 1)


def _catalog_instrument_type(raw: Any, fallback: str) -> str:
    """Тип инструмента: собственное поле API, иначе тип метода-источника."""
    declared = _catalog_text(raw, "instrument_type").strip().lower()
    return declared or fallback


def instrument_list_to_catalog(items: Any, instrument_type: str) -> list[InstrumentCatalogEntry]:
    """``Share``/``Etf``/``Currency``/``Future`` из списка API → записи каталога.

    ``instrument_type`` — тип, с которым был вызван метод списка: он нужен как
    запасной вариант, если API не заполнил собственное поле ``instrument_type``.
    """
    entries: list[InstrumentCatalogEntry] = []
    for raw in items or []:
        uid = _catalog_text(raw, "uid")
        ticker = _catalog_text(raw, "ticker")
        if not uid or not ticker:
            continue
        entries.append(
            InstrumentCatalogEntry(
                uid=uid,
                ticker=ticker,
                class_code=_catalog_text(raw, "class_code"),
                name=_catalog_text(raw, "name"),
                lot_size=_catalog_lot(raw),
                currency=_catalog_text(raw, "currency").upper(),
                instrument_type=_catalog_instrument_type(raw, instrument_type),
                isin=_catalog_text(raw, "isin"),
                figi=_catalog_text(raw, "figi"),
                api_trade_available=_catalog_flag(raw, "api_trade_available_flag"),
                buy_available=_catalog_flag(raw, "buy_available_flag"),
                sell_available=_catalog_flag(raw, "sell_available_flag"),
                for_iis=_catalog_flag(raw, "for_iis_flag"),
                for_qual_investor=_catalog_flag(raw, "for_qual_investor_flag"),
                exchange=_catalog_text(raw, "exchange"),
                sector=_catalog_text(raw, "sector"),
                country_of_risk=_catalog_text(raw, "country_of_risk_name", "country_of_risk"),
                liquidity=_catalog_flag(raw, "liquidity_flag"),
                min_price_increment=_catalog_increment(raw),
            )
        )
    return entries


def instrument_short_to_catalog_entry(raw: Any) -> InstrumentCatalogEntry:
    """``InstrumentShort`` из ``FindInstrument`` → запись каталога.

    У ``InstrumentShort`` API не отдаёт валюту котировки и шаг цены: это
    подсказка для поиска. Точные ``currency``, ``lot`` и ``min_price_increment``
    приходят из ``GetInstrumentBy`` при добавлении инструмента в корзину.
    """
    kind = getattr(raw, "instrument_kind", None)
    instrument_type = str(getattr(kind, "name", "") or "").removeprefix("INSTRUMENT_TYPE_").lower()
    return InstrumentCatalogEntry(
        uid=_catalog_text(raw, "uid"),
        ticker=_catalog_text(raw, "ticker"),
        class_code=_catalog_text(raw, "class_code"),
        name=_catalog_text(raw, "name"),
        lot_size=_catalog_lot(raw),
        currency="",
        instrument_type=instrument_type or _catalog_instrument_type(raw, "share"),
        isin=_catalog_text(raw, "isin"),
        figi=_catalog_text(raw, "figi"),
        api_trade_available=_catalog_flag(raw, "api_trade_available_flag"),
        for_iis=_catalog_flag(raw, "for_iis_flag"),
        for_qual_investor=_catalog_flag(raw, "for_qual_investor_flag"),
    )


ORDER_STATUS_MAP: dict[int, OrderStatus] = {
    1: OrderStatus.FILLED,  # EXECUTION_REPORT_STATUS_FILL
    2: OrderStatus.REJECTED,  # EXECUTION_REPORT_STATUS_REJECTED
    3: OrderStatus.CANCELLED,  # EXECUTION_REPORT_STATUS_CANCELLED
    4: OrderStatus.ACCEPTED,  # EXECUTION_REPORT_STATUS_NEW
    5: OrderStatus.PARTIALLY_FILLED,  # EXECUTION_REPORT_STATUS_PARTIALLYFILL
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
        client_order_id=str(getattr(response, "order_request_id", "") or client_order_id),
        status=order_status_to_domain(getattr(response, "execution_report_status", 0)),
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
