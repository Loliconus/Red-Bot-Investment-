"""Контрактные тесты адаптеров T-Invest на песочнице.

Запускаются **только** явно: ``pytest -m sandbox``. Требуют токен песочницы
(в keyring или ``REDBOT__TBANK__API_TOKEN``) и установленный SDK.

Тесты здесь нужны не для покрытия, а для проверки того, чего нельзя проверить
на фейках: реальная схема protobuf, лимиты API, поведение песочницы при
некорректном запросе.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from config.enums import ExecutionMode
from config.settings import Settings

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.asyncio,
]


def _sdk_available() -> bool:
    try:
        import t_tech.invest.grpc  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def sandbox_settings() -> Settings:
    return Settings(
        execution_mode=ExecutionMode.SANDBOX,
        tbank={"account_id": "sandbox-account"},
    )


@pytest.mark.skipif(not _sdk_available(), reason="SDK t-tech-investments не установлен")
async def test_sandbox_adapters_connect(sandbox_settings: Settings) -> None:
    """Минимальная проверка: адаптеры поднимаются и отдают список инструментов."""
    from adapters.driven.sandbox.sandbox_adapter import create_sandbox_adapters

    market_data, broker = await create_sandbox_adapters(sandbox_settings)
    try:
        instruments = await broker.list_instruments()
        assert isinstance(instruments, list)
    finally:
        await market_data.aclose()
        await broker.aclose()


@pytest.mark.skipif(not _sdk_available(), reason="SDK t-tech-investments не установлен")
async def test_sandbox_market_data_returns_candles(sandbox_settings: Settings) -> None:
    from adapters.driven.sandbox.sandbox_adapter import create_sandbox_adapters

    market_data, broker = await create_sandbox_adapters(sandbox_settings)
    try:
        instruments = await broker.list_instruments()
        if not instruments:
            pytest.skip("в песочнице нет доступных инструментов")

        instrument: Any = instruments[0]
        now = market_data._channel and __import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        )
        candles = await market_data.get_candles(
            instrument,
            __import__("core.domain.enums", fromlist=["Timeframe"]).Timeframe.H1,
            from_=now - timedelta(days=7),
            to=now,
        )
        for candle in candles:
            assert candle.timeframe.value == "1h"
            assert candle.volume >= 0
            assert isinstance(candle.close, Decimal)
    finally:
        await market_data.aclose()
        await broker.aclose()
