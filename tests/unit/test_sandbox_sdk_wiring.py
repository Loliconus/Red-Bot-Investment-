"""Тесты wiring к SDK без сети и без установленного SDK."""

from __future__ import annotations

from typing import Any

import pytest

from adapters.driven.sandbox import sandbox_adapter
from adapters.driven.sandbox.sandbox_adapter import SandboxBrokerAdapter, SandboxChannel
from config.enums import ExecutionMode
from config.settings import Settings


class _FakeAsyncSandboxClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True


async def test_sandbox_channel_uses_supported_sdk_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def client_factory() -> type[_FakeAsyncSandboxClient]:
        return _FakeAsyncSandboxClient

    monkeypatch.setattr(sandbox_adapter, "_import_async_sandbox_client", client_factory)
    monkeypatch.setattr(sandbox_adapter, "configure_sdk_tls", lambda: None)

    settings = Settings(
        execution_mode=ExecutionMode.SANDBOX,
        tbank={"api_token": "", "account_id": ""},
    )
    channel = await SandboxChannel.create(settings)
    assert channel.target == "sandbox-invest-public-api.tbank.ru:443"
    client = channel._client
    assert client.kwargs == {"token": "", "app_name": "red-bot"}
    assert "channel_credentials" not in client.kwargs
    await channel.aclose()
    assert client.closed


async def test_sandbox_trading_uses_regular_orders_service() -> None:
    class Orders:
        async def post_order(self, *, request: object) -> str:
            captured["request"] = request
            return "sent-to-sandbox-orders"

    class SandboxOnly:
        async def post_sandbox_order(self, *, request: object) -> str:
            raise AssertionError("ordinary sandbox orders should use services.orders")

    class Services:
        orders = Orders()
        sandbox = SandboxOnly()

    class Channel:
        services = Services()

    captured: dict[str, object] = {}
    broker = SandboxBrokerAdapter(Channel(), account_id="sandbox-account")  # type: ignore[arg-type]
    result = await broker._submit_order("request")
    assert result == "sent-to-sandbox-orders"
    assert captured["request"] == "request"
