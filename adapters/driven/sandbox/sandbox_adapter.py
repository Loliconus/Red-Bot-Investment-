"""Адаптер контура песочницы T-Invest.

Песочница — не «бэктест» и не игрушка: это полноценный контур API с виртуальными
деньгами, где проверяется именно **интеграция** — форматы запросов, обработка
ответов, идемпотентность, работа со счетами, поведение стримов.

Особенности песочницы, которые нужно помнить:
* плечо 2, комиссия 0.05%, налогов нет;
* маркет-заявки исполняются по last price;
* счёт живёт 3 месяца, лимит 30 млн ₽;
* результаты песочницы **не** заменяют walk-forward бэктест.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter, retry_read_safe
from adapters.driven.tbank.grpc_client import TInvestChannel
from adapters.driven.tbank.mappers import money_value_to_decimal
from adapters.driven.tbank.market_data_adapter import TBankMarketDataAdapter
from adapters.driven.tbank.tls import configure_sdk_tls
from config.enums import ExecutionMode
from config.settings import Settings

logger = structlog.get_logger(__name__)


class SandboxChannel(TInvestChannel):
    """Канал к ``sandbox-invest-public-api.tbank.ru:443``."""

    @classmethod
    async def create(
        cls,
        settings: Settings,
        mode: ExecutionMode | None = None,
    ) -> SandboxChannel:
        # mode принимается для совместимости сигнатуры с TInvestChannel.create:
        # sandbox-канал всегда указывает на песочный target независимо от контура.
        sandbox_client = _import_async_sandbox_client()
        configure_sdk_tls()

        channel = cls(
            target=settings.tbank.grpc_target_sandbox,
            token=settings.tbank.api_token.get_secret_value(),
        )
        # SDK сам фиксирует официальный sandbox target; channel_credentials не
        # является поддерживаемым аргументом его клиента.
        client = sandbox_client(token=channel._token, app_name="red-bot")
        await channel._enter_client(client)
        logger.info("sandbox_channel_created", target=channel._target)
        return channel


def _import_async_sandbox_client() -> Any:
    try:
        from t_tech.invest.grpc import AsyncSandboxClient  # pyright: ignore
    except ImportError as exc:  # pragma: no cover
        msg = (
            "Пакет t-tech-investments не установлен. Установите его из реестра Т-Банка:\n"
            "  pip install t-tech-investments "
            "--index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
        )
        raise RuntimeError(msg) from exc
    return AsyncSandboxClient


class SandboxMarketDataAdapter(TBankMarketDataAdapter):
    """Рыночные данные песочницы: контракт тот же, что и в боевом контуре."""


class SandboxBrokerAdapter(TBankBrokerAdapter):
    """Торговля идет через обычные services.orders/operations на sandbox target.

    Только управление тестовыми счетами и пополнение используют SandboxService.
    """

    async def get_sandbox_accounts(self) -> list[dict[str, Any]]:
        """Запрашивает счета реального sandbox API; ошибки подключения не маскируются."""
        from t_tech.invest.grpc.schemas import GetAccountsRequest

        response = await retry_read_safe(
            lambda: self._channel.services.sandbox.get_sandbox_accounts(
                request=GetAccountsRequest()
            )
        )
        return [
            {
                "id": str(acc.id),
                "name": str(getattr(acc, "name", "") or "Песочница"),
                "status": int(getattr(acc, "status", 0)),
                "type": int(getattr(acc, "type", 0)),
                "is_current": str(acc.id) == self._account_id,
            }
            for acc in getattr(response, "accounts", []) or []
        ]

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Общий интерфейс списка счетов для sandbox/live resolver."""
        return await self.get_sandbox_accounts()

    async def ensure_managed_sandbox_account(
        self,
        *,
        preferred_account_id: str | None = None,
        selector: Any = None,
    ) -> str:
        """Выбирает открытый счёт или однократно создаёт sandbox-счёт при пустом списке."""
        from application.use_cases.select_account import resolve_managed_account_id
        from config.enums import ExecutionMode

        selected_id = await resolve_managed_account_id(
            self,
            mode=ExecutionMode.SANDBOX,
            requested_account_id=self._account_id or None,
            preferred_account_id=preferred_account_id,
            selector=selector,
        )
        self.configure_managed_account(selected_id)
        return selected_id

    async def open_sandbox_account(self, name: str = "Red-Bot Sandbox") -> str:
        """Создаёт счёт через SandboxService. Запрос не поддерживает идемпотентность."""
        from t_tech.invest.grpc.sandbox import OpenSandboxAccountRequest

        response = await self._channel.services.sandbox.open_sandbox_account(
            request=OpenSandboxAccountRequest(name=name)
        )
        account_id = str(response.account_id)
        if not account_id:
            raise RuntimeError("SandboxService вернул пустой account_id")
        logger.info("sandbox_account_opened")
        return account_id

    async def close_sandbox_account(self, account_id: str) -> None:
        """Закрывает счёт в sandbox API ровно одним запросом."""
        from t_tech.invest.grpc.sandbox import CloseSandboxAccountRequest

        request = CloseSandboxAccountRequest(account_id=account_id)
        await self._channel.services.sandbox.close_sandbox_account(request=request)
        logger.info("sandbox_account_closed")

    async def sandbox_pay_in(
        self, account_id: str, amount: Decimal, currency: str = "rub"
    ) -> Decimal:
        """Пополняет sandbox API без повторов: у SandboxPayIn нет idempotency key."""
        from t_tech.invest.grpc.sandbox import SandboxPayInRequest
        from t_tech.invest.utils import decimal_to_money

        if amount <= 0:
            raise ValueError("Сумма пополнения должна быть больше нуля")
        self._assert_managed_account(account_id)
        request = SandboxPayInRequest(
            account_id=account_id,
            amount=decimal_to_money(amount, currency),
        )
        response = await self._channel.services.sandbox.sandbox_pay_in(request=request)
        balance = money_value_to_decimal(getattr(response, "balance", None))
        logger.info("sandbox_pay_in_completed", balance=str(balance))
        return balance


async def create_sandbox_adapters(
    settings: Settings,
    *,
    managed_account_id: str | None = None,
) -> tuple[SandboxMarketDataAdapter, SandboxBrokerAdapter]:
    """Создаёт пару адаптеров песочницы."""
    channel = await SandboxChannel.create(settings)
    market_data = SandboxMarketDataAdapter(channel)
    broker = SandboxBrokerAdapter(
        channel, account_id=managed_account_id or settings.tbank.account_id
    )
    return market_data, broker


__all__ = [
    "SandboxBrokerAdapter",
    "SandboxChannel",
    "SandboxMarketDataAdapter",
    "create_sandbox_adapters",
]
