"""Интеграционные тесты composition root."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from application.composition import (
    AppContext,
    build_backtest_adapters,
    build_context,
    build_storage,
    load_saved_execution_mode,
)
from config.enums import ExecutionMode
from config.settings import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "execution_mode": ExecutionMode.BACKTEST,
        "tbank": {"api_token": "t", "account_id": "a"},
        "storage": {"data_dir": str(tmp_path / "data")},
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_build_context_backtest_creates_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)

    try:
        assert isinstance(context, AppContext)
        assert context.config.version >= 1
        assert context.started_at is not None
        assert context.kill_switch is not None
        assert not context.kill_switch.is_engaged
        assert context.archive is not None
        assert context.benchmark is None
    finally:
        await context.aclose()


async def test_saved_execution_mode_overrides_bootstrap_fallback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository, _archive = build_storage(settings)
    await repository.set_operational_value("execution_mode", ExecutionMode.LIVE.value)
    await repository.aclose()

    assert await load_saved_execution_mode(settings) is ExecutionMode.LIVE


async def test_build_storage_creates_files_and_archive(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repository, archive = build_storage(settings)

    try:
        assert await asyncio.to_thread(Path(settings.storage.data_dir).exists)
        assert await asyncio.to_thread((Path(settings.storage.data_dir) / "redbot.duckdb").exists)
        assert await asyncio.to_thread((Path(settings.storage.data_dir) / "archive").exists)

        usage = await archive.usage_by_layer()
        assert set(usage) == {"hot", "warm", "cold"}
        assert await repository.get_active_strategy_config() is None
    finally:
        await repository.aclose()


async def test_build_backtest_adapters_returns_ports(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    from adapters.driven.backtest.replay_adapter import BacktestReplayAdapter
    from adapters.driven.backtest.simulated_broker import SimulatedBroker
    from core.ports.broker import OrderExecutionPort
    from core.ports.market_data import MarketDataPort

    market_data, broker = build_backtest_adapters(settings)
    assert isinstance(market_data, BacktestReplayAdapter)
    assert isinstance(broker, SimulatedBroker)
    assert isinstance(market_data, MarketDataPort)
    assert isinstance(broker, OrderExecutionPort)


async def test_context_persists_instruments(tmp_path: Path) -> None:
    from application.use_cases.bootstrap_database import seed_instrument

    settings = _settings(tmp_path)
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)
    try:
        instrument = seed_instrument("uid-sber", "SBER", 10)
        await context.repository.save_instrument(instrument)
        benchmark = seed_instrument("uid-imoex", "IMOEX", 1, is_benchmark=True)
        await context.repository.save_instrument(benchmark)

        context.instruments = await context.repository.list_instruments()
        context.benchmark = benchmark

        assert len(context.tradable_instruments) == 1
        assert context.tradable_instruments[0].ticker == "SBER"
        assert context.benchmark.ticker == "IMOEX"
    finally:
        await context.aclose()


async def test_context_preview_size_respects_config(tmp_path: Path) -> None:
    from application.use_cases.bootstrap_database import seed_instrument

    settings = _settings(tmp_path)
    context = await build_context(settings, mode=ExecutionMode.BACKTEST)
    try:
        instrument = seed_instrument("uid", "SBER", 10)
        from core.domain.entities import PortfolioState

        context.portfolio = PortfolioState(
            account_id="a",
            total_value=Decimal("0"),
            available_cash=Decimal("0"),
            positions_value=Decimal("0"),
            updated_at=context.clock.now(),
        )
        with pytest.raises(ValueError, match="Капитал"):
            context.preview_size(Decimal("100"), Decimal("95"), instrument)
    finally:
        await context.aclose()


async def test_repository_and_archive_share_one_pool(tmp_path: Path) -> None:
    """Архив обязан работать с тем же подключением, что и репозиторий."""
    settings = _settings(tmp_path)
    repository, archive = build_storage(settings)
    try:
        assert archive._pool is repository._pool
    finally:
        await repository.aclose()
