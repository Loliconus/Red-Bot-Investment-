"""Общие фикстуры тестов.

Принцип: ни один юнит-тест ядра не трогает сеть, диск и внешние библиотеки.
Для всего внешнего есть фейки из ``tests/fakes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from application.composition import AppContext
from application.events import EventBus
from application.kill_switch import KillSwitch
from config.enums import ExecutionMode
from config.settings import Settings, reset_settings_cache
from core.domain.entities import Instrument, PortfolioState
from core.domain.enums import Timeframe
from core.domain.value_objects import CandleSeries
from core.ports.clock import FrozenClock
from tests.fakes import (
    FakeArchive,
    FakeBroker,
    FakeMarketData,
    FakeNotifier,
    InMemoryRepository,
    make_candles,
    make_config,
    make_instrument,
    make_orderbook,
)

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_settings() -> Any:
    """Конфиг кешируется на процесс — сбрасываем между тестами."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        execution_mode=ExecutionMode.BACKTEST,
        tbank={
            "api_token": "test-token",
            "account_id": "test-account",
        },
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def instrument() -> Instrument:
    return make_instrument()


@pytest.fixture
def benchmark() -> Instrument:
    return make_instrument(uid="uid-imoex", ticker="IMOEX", lot_size=1, is_benchmark=True)


@pytest.fixture
def orderbook() -> Any:
    return make_orderbook()


@pytest.fixture
def config() -> Any:
    return make_config()


@pytest.fixture
def candles_m1() -> tuple[Any, ...]:
    """Минутки «почти без тренда»: цена рядом с VWAP, вход разрешён."""
    return make_candles(
        start=NOW - timedelta(minutes=119),
        count=120,
        timeframe=Timeframe.M1,
        base_price=Decimal("135"),
        step=Decimal("0.001"),
    )


@pytest.fixture
def candles_h1() -> tuple[Any, ...]:
    return make_candles(
        start=NOW - timedelta(hours=119),
        count=120,
        timeframe=Timeframe.H1,
        step=Decimal("0.3"),
    )


@pytest.fixture
def candles_d1() -> tuple[Any, ...]:
    return make_candles(
        start=NOW - timedelta(days=199),
        count=200,
        timeframe=Timeframe.D1,
        step=Decimal("0.4"),
    )


@pytest.fixture
def series_d1(candles_d1: tuple[Any, ...]) -> CandleSeries:
    return CandleSeries(timeframe=Timeframe.D1, candles=candles_d1)


@pytest.fixture
def series_h1(candles_h1: tuple[Any, ...]) -> CandleSeries:
    return CandleSeries(timeframe=Timeframe.H1, candles=candles_h1)


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState(
        account_id="test-account",
        total_value=Decimal("500000"),
        available_cash=Decimal("500000"),
        positions_value=Decimal("0"),
        updated_at=NOW,
    )


@pytest.fixture
def repository() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def market_data(
    instrument: Instrument,
    candles_d1: tuple[Any, ...],
    candles_h1: tuple[Any, ...],
    candles_m1: tuple[Any, ...],
    orderbook: Any,
) -> FakeMarketData:
    return FakeMarketData(
        {
            (instrument.uid, Timeframe.D1): candles_d1,
            (instrument.uid, Timeframe.H1): candles_h1,
            (instrument.uid, Timeframe.M1): candles_m1,
        },
        instruments={("SBER", "TQBR"): instrument},
        orderbook=orderbook,
        indicators={
            (instrument.uid, "sma", Timeframe.D1): {"sma": 90.0},
            (instrument.uid, "ema", Timeframe.D1): {"ema": 92.0},
            (instrument.uid, "rsi", Timeframe.H1): {"rsi": 35.0},
            (instrument.uid, "macd", Timeframe.H1): {"macd": 0.4, "signal": 0.2},
            (instrument.uid, "bollinger", Timeframe.H1): {"bb_lower": 95.0},
        },
    )


@pytest.fixture
def context(
    settings: Settings,
    clock: FrozenClock,
    market_data: FakeMarketData,
    broker: FakeBroker,
    repository: InMemoryRepository,
    archive: FakeArchive,
    notifier: FakeNotifier,
    event_bus: EventBus,
    config: Any,
    instrument: Instrument,
    portfolio: PortfolioState,
) -> AppContext:
    kill_switch = KillSwitch(clock=clock, event_bus=event_bus)
    return AppContext(
        settings=settings,
        market_data=market_data,
        broker=broker,
        repository=repository,
        archive=archive,
        clock=clock,
        notifier=notifier,
        event_bus=event_bus,
        config=config,
        instruments=[instrument],
        benchmark=None,
        portfolio=portfolio,
        kill_switch=kill_switch,
        started_at=clock.now(),
    )


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    target.mkdir(parents=True, exist_ok=True)
    return target
