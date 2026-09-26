"""Read-only контрактные проверки адаптеров T-Invest на sandbox API.

Запускаются явно: ``REDBOT_RUN_SANDBOX_TESTS=1 pytest -m sandbox``. Требуют
установленный SDK и токен в keyring или ``REDBOT_TBANK__API_TOKEN``. Тесты не создают счета, не пополняют
баланс и не отправляют/отменяют заявки.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.enums import ExecutionMode
from config.settings import Settings
from core.domain.enums import Timeframe

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("REDBOT_RUN_SANDBOX_TESTS") != "1",
        reason="Опциональные сетевые тесты: задайте REDBOT_RUN_SANDBOX_TESTS=1",
    ),
]


def _sdk_available() -> bool:
    try:
        import t_tech.invest.grpc  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def sandbox_settings() -> Settings:
    return Settings(execution_mode=ExecutionMode.SANDBOX)


@pytest.mark.skipif(not _sdk_available(), reason="SDK t-tech-investments не установлен")
async def test_sandbox_read_only_api_calls_and_response_fields(
    sandbox_settings: Settings,
) -> None:
    from adapters.driven.sandbox.sandbox_adapter import create_sandbox_adapters

    market_data, broker = await create_sandbox_adapters(sandbox_settings)
    try:
        accounts = await broker.get_sandbox_accounts()
        assert isinstance(accounts, list)

        instruments = await broker.list_instruments()
        assert isinstance(instruments, list)

        instrument = await market_data.resolve_instrument("SBER", "TQBR")
        now = datetime.now(tz=UTC)
        candles = await market_data.get_candles(
            instrument,
            Timeframe.D1,
            from_=now - timedelta(days=30),
            to=now,
        )
        assert isinstance(candles, list)
        for candle in candles:
            assert candle.timeframe is Timeframe.D1
            assert candle.timestamp.tzinfo is not None
            assert isinstance(candle.close, Decimal)

        book = await market_data.get_orderbook(instrument, depth=10)
        assert book.captured_at.tzinfo is not None
        assert all(level.quantity >= 0 for level in (*book.bids, *book.asks))

        # Этот ID нужен только для type checking: GetPortfolio прозванивается,
        # если пользователь явно указал свой реальный sandbox account_id.
        account_id = sandbox_settings.tbank.account_id
        if account_id:
            portfolio = await broker.get_portfolio()
            assert portfolio is not None
            assert portfolio.account_id == account_id
    finally:
        await market_data.aclose()
        await broker.aclose()
