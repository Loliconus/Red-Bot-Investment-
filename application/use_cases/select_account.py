"""Выбор управляемого брокерского счёта для активного контура."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import structlog

from config.enums import ExecutionMode

logger = structlog.get_logger(__name__)

OPEN_ACCOUNT_STATUS = 2  # AccountStatus.ACCOUNT_STATUS_OPEN
ACCOUNT_TYPE_PRIORITY: dict[int, int] = {
    1: 0,  # ACCOUNT_TYPE_TINKOFF — обычный брокерский счёт
    2: 1,  # ACCOUNT_TYPE_TINKOFF_IIS — ИИС
    3: 2,  # ACCOUNT_TYPE_INVEST_BOX — инвесткопилка
}
ACCOUNT_TYPE_LABELS: dict[int, str] = {
    1: "Брокерский",
    2: "ИИС",
    3: "Инвесткопилка",
}

AccountSelector = Callable[[Sequence[dict[str, Any]]], str]


class AccountSelectionRequired(RuntimeError):
    """Несколько равноприоритетных счетов требуют явного выбора пользователя."""

    def __init__(self, accounts: Sequence[dict[str, Any]]) -> None:
        self.accounts = tuple(accounts)
        super().__init__("Нужно выбрать счёт: запустите команду в интерактивном терминале")


async def resolve_managed_account_id(
    broker: Any,
    *,
    mode: ExecutionMode,
    requested_account_id: str | None = None,
    preferred_account_id: str | None = None,
    selector: AccountSelector | None = None,
    accounts: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Выбирает OPEN-счёт; не делает торговых операций и не пополняет баланс."""
    accounts = list(accounts) if accounts is not None else await broker.get_accounts()
    open_accounts = [
        account for account in accounts if int(account.get("status", 0)) == OPEN_ACCOUNT_STATUS
    ]

    if requested_account_id:
        selected = next(
            (account for account in open_accounts if account["id"] == requested_account_id),
            None,
        )
        if selected is None:
            raise ValueError(
                "Заданный account_id не найден среди открытых счетов выбранного контура"
            )
        return str(selected["id"])

    if preferred_account_id:
        selected = next(
            (account for account in open_accounts if account["id"] == preferred_account_id),
            None,
        )
        if selected is not None:
            return str(selected["id"])
        logger.warning("saved_account_preference_unavailable", mode=mode.value)

    if not open_accounts:
        if mode is not ExecutionMode.SANDBOX:
            raise RuntimeError("Не найдено открытых брокерских счетов. Проверьте доступ токена.")
        open_account = getattr(broker, "open_sandbox_account", None)
        if open_account is None:
            raise RuntimeError("Sandbox адаптер не умеет открыть sandbox-счёт")
        account_id = str(await open_account())
        if not account_id:
            raise RuntimeError("Sandbox API не вернул account_id открытого счёта")
        logger.info("sandbox_account_created_for_first_start")
        return account_id

    ranked_accounts = sorted(
        open_accounts,
        key=lambda account: (
            ACCOUNT_TYPE_PRIORITY.get(int(account.get("type", 0)), 99),
            str(account.get("name", "")).casefold(),
            str(account["id"]),
        ),
    )
    best_priority = ACCOUNT_TYPE_PRIORITY.get(int(ranked_accounts[0].get("type", 0)), 99)
    preferred_accounts = [
        account
        for account in ranked_accounts
        if ACCOUNT_TYPE_PRIORITY.get(int(account.get("type", 0)), 99) == best_priority
    ]

    if len(preferred_accounts) == 1:
        return str(preferred_accounts[0]["id"])
    if selector is None:
        raise AccountSelectionRequired(preferred_accounts)

    selected_id = selector(preferred_accounts)
    if not any(account["id"] == selected_id for account in preferred_accounts):
        raise ValueError("Выбранный account_id не входит в список доступных счетов")
    return selected_id
