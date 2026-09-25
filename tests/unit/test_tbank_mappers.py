"""Юнит-тесты мапперов и retry-политики T-Invest (без сети и SDK)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.driven.tbank.mappers import (
    TIMEFRAME_TO_API_INTERVAL,
    candle_to_domain,
    decimal_to_quotation,
    instrument_to_domain,
    lots_to_units,
    order_status_to_domain,
    orderbook_to_domain,
    proto_timestamp_to_datetime,
    quotation_to_decimal,
    units_to_lots,
)
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


def test_decimal_to_quotation_round_trip() -> None:
    value = Decimal("275.125")
    payload = decimal_to_quotation(value)
    restored = quotation_to_decimal(_quotation(**payload))
    assert restored == value


def test_proto_timestamp_to_datetime_is_utc() -> None:
    moment = proto_timestamp_to_datetime(SimpleNamespace(seconds=1_700_000_000, nanos=0))
    assert moment.tzinfo is not None
    assert moment == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_candle_to_domain_maps_fields() -> None:
    raw = SimpleNamespace(
        open=_quotation(100),
        high=_quotation(102),
        low=_quotation(98),
        close=_quotation(101, 500_000_000),
        volume=1234,
        time=SimpleNamespace(seconds=1_700_000_000, nanos=0),
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
        time=SimpleNamespace(seconds=1_700_000_000, nanos=0),
    )
    book = orderbook_to_domain(raw)
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


def test_timeframe_mapping_matches_api_intervals() -> None:
    assert TIMEFRAME_TO_API_INTERVAL[Timeframe.M1] == 1
    assert TIMEFRAME_TO_API_INTERVAL[Timeframe.H1] == 5
    assert TIMEFRAME_TO_API_INTERVAL[Timeframe.D1] == 8


def test_order_status_mapping() -> None:
    assert order_status_to_domain(1) is OrderStatus.FILLED
    assert order_status_to_domain(3) is OrderStatus.CANCELLED
    assert order_status_to_domain(999) is OrderStatus.UNKNOWN


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
        idempotency_key="key",
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_mutation_without_idempotency_key_is_never_retried() -> None:
    """Главное правило: мутация без ключа идемпотентности не повторяется."""
    calls = {"n": 0}

    async def operation() -> str:
        calls["n"] += 1
        raise _GrpcError("UNAVAILABLE")

    with pytest.raises(_GrpcError):
        await retry_async(
            operation,
            policy=RetryPolicy(max_attempts=5, base_delay=0.001),
            idempotency_key=None,
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
            idempotency_key="key",
        )


async def test_cancellation_is_propagated() -> None:
    async def operation() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(operation, idempotency_key="key")
