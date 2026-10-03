"""Юнит-тесты мапперов и retry-политики T-Invest без сетевого доступа."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.driven.tbank.mappers import (
    TIMEFRAME_TO_API_INTERVAL,
    TIMEFRAME_TO_INDICATOR_INTERVAL,
    TIMEFRAME_TO_SUBSCRIPTION_INTERVAL,
    candle_to_domain,
    datetime_to_proto_timestamp,
    decimal_to_quotation,
    instrument_to_domain,
    lots_to_units,
    order_status_to_domain,
    orderbook_to_domain,
    proto_timestamp_to_datetime,
    quotation_to_decimal,
    units_to_lots,
)
from adapters.driven.tbank.market_data_adapter import _parse_tech_analysis
from adapters.driven.tbank.retry import (
    RetryExhaustedError,
    RetryPolicy,
    classify_error,
    retry_async,
)
from core.domain.enums import OrderStatus, Timeframe


def _quotation(units: int, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano)


def test_quotation_to_decimal_handles_nano() -> None:
    assert quotation_to_decimal(_quotation(100, 250_000_000)) == Decimal("100.25")
    assert quotation_to_decimal(_quotation(0, 1)) == Decimal("0.000000001")


def test_quotation_to_decimal_handles_none() -> None:
    assert quotation_to_decimal(None) == Decimal("0")


def test_sdk_decimal_quotation_round_trip() -> None:
    value = Decimal("275.125")
    quotation = decimal_to_quotation(value)
    assert quotation_to_decimal(quotation) == value


def test_proto_timestamp_to_datetime_is_utc() -> None:
    moment = proto_timestamp_to_datetime(SimpleNamespace(seconds=1_700_000_000, nanos=0))
    assert moment.tzinfo is not None
    assert moment == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_sdk_datetime_is_used_for_request_timestamp() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert datetime_to_proto_timestamp(moment) == moment
    with pytest.raises(ValueError, match="Naive datetime"):
        datetime_to_proto_timestamp(datetime(2026, 1, 1))


def test_sdk_datetime_response_maps_to_utc() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    assert proto_timestamp_to_datetime(moment) == moment


def test_candle_to_domain_maps_fields() -> None:
    raw = SimpleNamespace(
        open=_quotation(100),
        high=_quotation(102),
        low=_quotation(98),
        close=_quotation(101, 500_000_000),
        volume=1234,
        time=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
    )
    candle = candle_to_domain(raw, Timeframe.H1)
    assert candle.close == Decimal("101.5")
    assert candle.volume == 1234
    assert candle.high == Decimal("102")
    assert candle.timeframe is Timeframe.H1


def test_orderbook_to_domain_builds_levels() -> None:
    raw = SimpleNamespace(
        bids=[SimpleNamespace(price=_quotation(99), quantity=10)],
        asks=[SimpleNamespace(price=_quotation(101), quantity=20)],
        orderbook_ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    book = orderbook_to_domain(raw)
    assert book.captured_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert book.spread == Decimal("2")
    assert book.imbalance < 0


def test_instrument_to_domain_maps_lot() -> None:
    raw = SimpleNamespace(uid="uid", ticker="SBER", class_code="TQBR", lot=10, currency="rub")
    instrument = instrument_to_domain(raw)
    assert instrument.lot_size == 10
    assert instrument.currency == "RUB"
    assert not instrument.is_benchmark


def test_lots_conversion() -> None:
    assert lots_to_units(3, 10) == 30
    assert units_to_lots(35, 10) == 3


def test_timeframe_mapping_matches_named_sdk_enums() -> None:
    assert TIMEFRAME_TO_API_INTERVAL == {
        Timeframe.M1: "CANDLE_INTERVAL_1_MIN",
        Timeframe.H1: "CANDLE_INTERVAL_HOUR",
        Timeframe.D1: "CANDLE_INTERVAL_DAY",
    }
    assert TIMEFRAME_TO_SUBSCRIPTION_INTERVAL[Timeframe.H1] == "SUBSCRIPTION_INTERVAL_ONE_HOUR"
    assert TIMEFRAME_TO_INDICATOR_INTERVAL[Timeframe.D1] == "INDICATOR_INTERVAL_ONE_DAY"


def test_order_status_mapping() -> None:
    assert order_status_to_domain(1) is OrderStatus.FILLED
    assert order_status_to_domain(2) is OrderStatus.REJECTED
    assert order_status_to_domain(3) is OrderStatus.CANCELLED
    assert order_status_to_domain(4) is OrderStatus.ACCEPTED
    assert order_status_to_domain(5) is OrderStatus.PARTIALLY_FILLED
    assert order_status_to_domain(999) is OrderStatus.UNKNOWN


def test_tech_analysis_uses_sdk_response_fields_and_keeps_decimal() -> None:
    item = SimpleNamespace(
        lower_band=_quotation(90, 250_000_000),
        middle_band=_quotation(100),
        upper_band=_quotation(110, 750_000_000),
        macd=_quotation(2, 500_000_000),
        signal=_quotation(1),
    )
    response = SimpleNamespace(technical_indicators=[item])
    bands = _parse_tech_analysis(response, "bb")
    assert bands == {
        "bb_lower": Decimal("90.25"),
        "bb_middle": Decimal("100"),
        "bb_upper": Decimal("110.75"),
    }
    macd = _parse_tech_analysis(response, "macd")
    assert macd["histogram"] == Decimal("1.5")


# ------------------------------------------------------------------ retry
class _GrpcError(Exception):
    def __init__(self, code_name: str) -> None:
        super().__init__(code_name)
        self.code = SimpleNamespace(name=code_name)


def test_classify_error_codes() -> None:
    assert classify_error(_GrpcError("UNAVAILABLE")) == "transient"
    assert classify_error(_GrpcError("RESOURCE_EXHAUSTED")) == "rate_limited"
    assert classify_error(_GrpcError("INVALID_ARGUMENT")) == "permanent"
    assert classify_error(ValueError("обычная")) == "permanent"


async def test_retry_read_succeeds_after_transient() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _GrpcError("UNAVAILABLE")
        return "ok"

    result = await retry_async(
        operation,
        policy=RetryPolicy(max_attempts=5, base_delay=0.001, jitter=False),
        idempotency_key="read-only",
        safe_to_retry=True,
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_mutation_with_idempotency_key_is_not_blindly_retried() -> None:
    """Idempotency key does not authorize a blind retry after uncertain outcome."""
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise _GrpcError("UNAVAILABLE")

    with pytest.raises(_GrpcError):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=5, base_delay=0.001),
            idempotency_key="request-id",
        )
    assert calls["n"] == 1


async def test_permanent_error_is_not_retried() -> None:
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise _GrpcError("INVALID_ARGUMENT")

    with pytest.raises(_GrpcError):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=3, base_delay=0.001),
            idempotency_key="key",
        )
    assert calls["n"] == 1


async def test_retry_exhausted_after_limit() -> None:
    async def operation() -> str:
        raise _GrpcError("UNAVAILABLE")

    with pytest.raises(RetryExhaustedError):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=2, base_delay=0.001, jitter=False),
            idempotency_key="read-only",
            safe_to_retry=True,
        )


async def test_cancellation_is_propagated() -> None:
    async def operation() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(operation, idempotency_key="key")
