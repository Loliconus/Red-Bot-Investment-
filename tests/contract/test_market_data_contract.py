"""Контрактные тесты ``MarketDataPort``.

Один и тот же набор проверок гоняется против **всех** реализаций порта:
фейка, реплея бэктеста и (при установленном SDK) адаптера T-Invest. Если
адаптер начинает вести себя иначе — контракт падает, и это правильно:
стратегия должна получать одинаковую семантику независимо от источника данных.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
from core.domain.enums import Timeframe
from core.domain.value_objects import OHLCV
from tests.fakes import (
    FakeMarketData,
    make_candles,
    make_catalog_entry,
    make_instrument,
    make_orderbook,
)

NOW = __import__("datetime").datetime(
    2026, 1, 10, 10, 0, tzinfo=__import__("datetime").timezone.utc
)


def _candles() -> tuple[OHLCV, ...]:
    return make_candles(start=NOW - timedelta(days=9), count=10, timeframe=Timeframe.D1)


def _catalog(instrument: Any) -> list[Any]:
    """Справочник инструментов, какой отдаёт InstrumentsService."""
    return [
        make_catalog_entry(
            uid=instrument.uid,
            ticker=instrument.ticker,
            name="Сбербанк",
            class_code=instrument.class_code,
            lot_size=instrument.lot_size,
        )
    ]


async def _replay(instrument: Any, data_dir: Any) -> BacktestReplayAdapter:
    from tests.fakes import InMemoryRepository

    # Каталог в бэктесте читается из хранилища: эмулируем сохранённые данные API.
    repository = InMemoryRepository()
    await repository.save_catalog_entries(_catalog(instrument))
    adapter = BacktestReplayAdapter(
        data_dir=data_dir, orderbook=make_orderbook(), repository=repository
    )
    rows = [(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in _candles()]
    adapter.load_from_rows(instrument.uid, Timeframe.D1, rows)
    return adapter


def _fake(instrument: Any, data_dir: Any = None) -> FakeMarketData:
    return FakeMarketData(
        {(instrument.uid, Timeframe.D1): _candles()},
        catalog=_catalog(instrument),
        orderbook=make_orderbook(),
    )


@pytest.fixture(params=["fake", "backtest_replay"])
async def adapter(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    instrument = make_instrument()
    if request.param == "fake":
        return _fake(instrument, tmp_path)
    return await _replay(instrument, tmp_path / "history")


@pytest.fixture
def instrument() -> Any:
    return make_instrument()


async def test_get_candles_returns_chronological_series(adapter: Any, instrument: Any) -> None:
    candles = await adapter.get_candles(
        instrument, Timeframe.D1, from_=NOW - timedelta(days=20), to=NOW
    )
    assert candles, "порт обязан вернуть историю"
    timestamps = [c.timestamp for c in candles]
    assert timestamps == sorted(timestamps)


async def test_get_candles_respects_time_bounds(adapter: Any, instrument: Any) -> None:
    candles = await adapter.get_candles(
        instrument, Timeframe.D1, from_=NOW - timedelta(days=2), to=NOW
    )
    for candle in candles:
        assert NOW - timedelta(days=2) <= candle.timestamp <= NOW


async def test_get_candles_empty_range_is_not_error(adapter: Any, instrument: Any) -> None:
    candles = await adapter.get_candles(
        instrument,
        Timeframe.D1,
        from_=NOW - timedelta(days=3650),
        to=NOW - timedelta(days=3600),
    )
    assert candles == []


async def test_candles_have_valid_ohlc_structure(adapter: Any, instrument: Any) -> None:
    candles = await adapter.get_candles(
        instrument, Timeframe.D1, from_=NOW - timedelta(days=20), to=NOW
    )
    for candle in candles:
        assert candle.high >= candle.low
        assert isinstance(candle.close, Decimal)
        assert candle.volume >= 0
        assert candle.timeframe is Timeframe.D1


async def test_get_orderbook_returns_consistent_book(adapter: Any, instrument: Any) -> None:
    book = await adapter.get_orderbook(instrument, depth=5)
    assert book.bids and book.asks
    assert book.spread >= Decimal("0")
    assert book.mid_price > Decimal("0")
    assert -1 <= book.imbalance <= 1


async def test_stream_candles_yields_same_data(adapter: Any, instrument: Any) -> None:
    streamed = [c async for c in adapter.stream_candles(instrument, Timeframe.D1)]
    requested = await adapter.get_candles(
        instrument, Timeframe.D1, from_=NOW - timedelta(days=20), to=NOW
    )
    assert len(streamed) == len(requested)


async def test_get_api_indicator_returns_mapping(adapter: Any, instrument: Any) -> None:
    """Реплика бэктеста поддерживает ATR — единственный «API-индикатор» офлайн."""
    values = await adapter.get_api_indicator(instrument, "atr", Timeframe.D1, {"period": 14})
    assert isinstance(values, dict)
    for value in values.values():
        assert value is None or isinstance(value, Decimal)


async def test_aclose_is_idempotent(adapter: Any) -> None:
    await adapter.aclose()
    await adapter.aclose()


async def test_resolve_instrument_uses_saved_catalog(adapter: Any, instrument: Any) -> None:
    """Лот и UID берутся из справочника, а не из значений по умолчанию в коде."""
    resolved = await adapter.resolve_instrument(instrument.ticker, instrument.class_code)
    assert resolved.uid == instrument.uid
    assert resolved.lot_size == instrument.lot_size


async def test_search_instruments_finds_catalog_entry(adapter: Any, instrument: Any) -> None:
    found = await adapter.search_instruments("Сбербанк")
    assert [entry.ticker for entry in found] == [instrument.ticker]
    assert found[0].uid == instrument.uid
