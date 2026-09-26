"""Offline tests for account resolution and persistent per-mode defaults."""

from __future__ import annotations

import pytest

from application.use_cases.manage_app_config import (
    account_setting_key,
    complete_sandbox_account_creation,
    load_default_mode,
    load_preferred_account_id,
    prepare_sandbox_account_creation,
    save_default_mode,
    save_preferred_account_id,
)
from application.use_cases.select_account import (
    AccountSelectionRequired,
    resolve_managed_account_id,
)
from config.enums import ExecutionMode
from tests.fakes import InMemoryRepository


class AccountsBroker:
    def __init__(self, accounts: list[dict[str, object]]) -> None:
        self.accounts = accounts
        self.open_calls = 0

    async def get_accounts(self) -> list[dict[str, object]]:
        return [dict(account) for account in self.accounts]

    async def open_sandbox_account(self) -> str:
        self.open_calls += 1
        return "new-sandbox-account"


def account(account_id: str, account_type: int, *, status: int = 2) -> dict[str, object]:
    return {"id": account_id, "type": account_type, "status": status, "name": account_id}


async def test_account_resolution_prefers_regular_brokerage_then_iis_then_invest_box() -> None:
    broker = AccountsBroker(
        [account("invest-box", 3), account("iis", 2), account("regular", 1)]
    )
    selected = await resolve_managed_account_id(broker, mode=ExecutionMode.SANDBOX)
    assert selected == "regular"


async def test_account_resolution_ignores_closed_accounts() -> None:
    broker = AccountsBroker([account("closed", 1, status=3), account("iis", 2)])
    selected = await resolve_managed_account_id(broker, mode=ExecutionMode.LIVE)
    assert selected == "iis"


async def test_equal_priority_requires_and_accepts_explicit_selection() -> None:
    broker = AccountsBroker([account("first", 1), account("second", 1)])
    with pytest.raises(AccountSelectionRequired) as error:
        await resolve_managed_account_id(broker, mode=ExecutionMode.LIVE)
    assert {item["id"] for item in error.value.accounts} == {"first", "second"}

    selected = await resolve_managed_account_id(
        broker,
        mode=ExecutionMode.LIVE,
        selector=lambda choices: str(choices[1]["id"]),
    )
    assert selected == "second"


async def test_explicit_and_saved_ids_must_be_open_accounts() -> None:
    broker = AccountsBroker([account("open", 1), account("closed", 1, status=3)])
    assert (
        await resolve_managed_account_id(
            broker,
            mode=ExecutionMode.LIVE,
            requested_account_id="open",
            preferred_account_id="missing",
        )
        == "open"
    )
    with pytest.raises(ValueError, match="открытых счетов"):
        await resolve_managed_account_id(
            broker, mode=ExecutionMode.LIVE, requested_account_id="closed"
        )


async def test_saved_preference_precedes_type_heuristic() -> None:
    broker = AccountsBroker([account("regular", 1), account("chosen-iis", 2)])
    selected = await resolve_managed_account_id(
        broker, mode=ExecutionMode.SANDBOX, preferred_account_id="chosen-iis"
    )
    assert selected == "chosen-iis"


async def test_empty_sandbox_opens_one_account_without_pay_in() -> None:
    broker = AccountsBroker([])
    selected = await resolve_managed_account_id(broker, mode=ExecutionMode.SANDBOX)
    assert selected == "new-sandbox-account"
    assert broker.open_calls == 1


async def test_empty_live_account_list_fails_without_creating_anything() -> None:
    broker = AccountsBroker([])
    with pytest.raises(RuntimeError, match="открытых брокерских счетов"):
        await resolve_managed_account_id(broker, mode=ExecutionMode.LIVE)
    assert broker.open_calls == 0


async def test_uncertain_sandbox_open_is_not_repeated_blindly() -> None:
    repository = InMemoryRepository()
    await prepare_sandbox_account_creation(
        repository,
        has_open_account=False,
        explicit_account_id=None,
    )
    assert repository.operational_values["sandbox_account_creation_pending"] == "true"

    with pytest.raises(RuntimeError, match="Повторное открытие запрещено"):
        await prepare_sandbox_account_creation(
            repository,
            has_open_account=False,
            explicit_account_id=None,
        )

    await prepare_sandbox_account_creation(
        repository,
        has_open_account=True,
        explicit_account_id=None,
    )
    await complete_sandbox_account_creation(repository)
    assert repository.operational_values["sandbox_account_creation_pending"] == "false"


async def test_mode_and_account_defaults_are_stored_separately() -> None:
    repository = InMemoryRepository()
    await save_default_mode(repository, ExecutionMode.SANDBOX)
    await save_preferred_account_id(repository, ExecutionMode.SANDBOX, "sandbox-id")
    await save_preferred_account_id(repository, ExecutionMode.LIVE, "live-id")

    assert await load_default_mode(repository, ExecutionMode.BACKTEST) is ExecutionMode.SANDBOX
    assert await load_preferred_account_id(repository, ExecutionMode.SANDBOX) == "sandbox-id"
    assert await load_preferred_account_id(repository, ExecutionMode.LIVE) == "live-id"
    assert account_setting_key(ExecutionMode.SANDBOX) != account_setting_key(ExecutionMode.LIVE)


async def test_auto_preference_suppresses_legacy_account_fallback() -> None:
    repository = InMemoryRepository()
    await repository.set_operational_value("managed_account_id", "old-shared-id")
    await save_preferred_account_id(repository, ExecutionMode.SANDBOX, "")
    assert await load_preferred_account_id(repository, ExecutionMode.SANDBOX) is None
