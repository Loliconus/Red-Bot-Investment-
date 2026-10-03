"""Account API request/model wiring with stub SDK modules and no network."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from adapters.driven.sandbox.sandbox_adapter import SandboxBrokerAdapter
from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter


class GetAccountsRequest:
    pass


def _install_sdk_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("t_tech", "t_tech.invest", "t_tech.invest.grpc"):
        module = ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, name, module)
    schemas = ModuleType("t_tech.invest.grpc.schemas")
    schemas.GetAccountsRequest = GetAccountsRequest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "t_tech.invest.grpc.schemas", schemas)


class AccountsService:
    async def get_accounts(self, *, request: object) -> object:
        assert type(request) is GetAccountsRequest
        return self.response()

    def response(self) -> object:
        return SimpleNamespace(
            accounts=[SimpleNamespace(id="account-123", name="Основной", status=2, type=1)]
        )


class LiveChannel:
    services = SimpleNamespace(users=AccountsService())


class SandboxChannel:
    services = SimpleNamespace(sandbox=SimpleNamespace())


async def test_live_account_list_uses_sdk_request_model_and_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_schema(monkeypatch)
    broker = TBankBrokerAdapter(LiveChannel(), account_id="")  # type: ignore[arg-type]
    accounts: list[dict[str, Any]] = await broker.get_accounts()
    assert accounts == [
        {
            "id": "account-123",
            "name": "Основной",
            "status": 2,
            "type": 1,
            "is_current": False,
        }
    ]


async def test_sandbox_account_list_uses_sandbox_service_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_schema(monkeypatch)

    class SandboxService:
        async def get_sandbox_accounts(self, *, request: object) -> object:
            assert type(request) is GetAccountsRequest
            return SimpleNamespace(
                accounts=[SimpleNamespace(id="sandbox-123", name="Песочница", status=2, type=1)]
            )

    channel = SandboxChannel()
    channel.services.sandbox = SandboxService()
    broker = SandboxBrokerAdapter(channel, account_id="")  # type: ignore[arg-type]
    assert await broker.get_accounts() == [
        {
            "id": "sandbox-123",
            "name": "Песочница",
            "status": 2,
            "type": 1,
            "is_current": False,
        }
    ]
