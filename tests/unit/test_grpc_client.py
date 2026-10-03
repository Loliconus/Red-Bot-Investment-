"""Offline SDK-client wiring checks; no connection to T-Invest is made."""

from __future__ import annotations

from typing import Any

import pytest

from adapters.driven.tbank import grpc_client
from config.enums import ExecutionMode
from config.settings import Settings


class FakeAsyncClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: Any) -> None:
        self.closed = True


async def test_explicit_execution_mode_selects_official_live_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grpc_client, "_import_async_client", lambda: FakeAsyncClient)
    monkeypatch.setattr(grpc_client, "configure_sdk_tls", lambda: None)
    settings = Settings(
        execution_mode=ExecutionMode.SANDBOX,
        tbank={"api_token": "", "account_id": ""},
    )

    channel = await grpc_client.create_channel(settings, mode=ExecutionMode.LIVE)
    client = channel._client
    assert client.kwargs["target"] == "invest-public-api.tbank.ru:443"
    assert client.kwargs["app_name"] == "red-bot"
    await channel.aclose()
    assert client.closed
