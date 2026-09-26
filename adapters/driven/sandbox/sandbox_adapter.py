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

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter, retry_read_safe
from adapters.driven.tbank.grpc_client import TInvestChannel
from adapters.driven.tbank.mappers import (
    NANO,
    money_value_to_decimal,
    portfolio_response_to_domain,
)
from adapters.driven.tbank.market_data_adapter import TBankMarketDataAdapter
from adapters.driven.tbank.retry import retry_async
from adapters.driven.tbank.tls import create_ssl_channel_credentials, resolve_ca_path
from config.settings import Settings
from core.domain.entities import PortfolioState, Position

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


class SandboxChannel(TInvestChannel):
    """Канал к ``sandbox-invest-public-api.tbank.ru:443``."""

    @classmethod
    async def create(cls, settings: Settings) -> SandboxChannel:
        sandbox_client = _import_async_sandbox_client()
        ca_path = resolve_ca_path(settings.tbank.ca_bundle_path)
        credentials = create_ssl_channel_credentials(
            ca_path, insecure_dev_only=settings.tbank.insecure_tls_dev_only
        )

        channel = cls(
            target=settings.tbank.grpc_target_sandbox,
            token=settings.tbank.api_token.get_secret_value(),
            ca_path=ca_path,
        )
        client = sandbox_client(
            token=channel._token,
            target=settings.tbank.grpc_target_sandbox,
            channel_credentials=credentials,
        )
        services = await client.__aenter__()
        channel._client = client
        channel._services = services
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
    """Исполнение в песочнице. Заявки и счета обслуживаются через services.sandbox."""

    @property
    def _orders(self) -> Any:
        # В песочнице T-Invest операции выставления ордеров принадлежат сервису sandbox
        return self._channel.services.sandbox

    async def get_sandbox_accounts(self) -> list[dict[str, Any]]:
        """Возвращает список всех счетов пользователя в песочнице."""
        from t_tech.invest.grpc.schemas import GetAccountsRequest

        try:
            response = await retry_read_safe(
                lambda: self._channel.services.sandbox.get_sandbox_accounts(
                    request=GetAccountsRequest()
                )
            )
            accounts: list[dict[str, Any]] = []
            for acc in getattr(response, "accounts", []) or []:
                accounts.append(
                    {
                        "id": str(acc.id),
                        "name": str(getattr(acc, "name", "") or "Песочница"),
                        "status": int(getattr(acc, "status", 1)),
                        "type": int(getattr(acc, "type", 1)),
                        "is_current": str(acc.id) == self._account_id,
                    }
                )
            return accounts
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_sandbox_accounts_failed", error=str(exc))
            return []

    async def open_sandbox_account(self, name: str = "Red-Bot Sandbox") -> str:
        """Создаёт счёт в песочнице."""
        from t_tech.invest.grpc.schemas import OpenSandboxAccountRequest

        try:
            request = OpenSandboxAccountRequest(name=name)
        except TypeError:
            request = OpenSandboxAccountRequest()

        response = await retry_async(
            lambda: self._channel.services.sandbox.open_sandbox_account(request=request),
            idempotency_key=f"open-{int(datetime.now(tz=UTC).timestamp())}",
            operation_name="open_sandbox_account",
        )
        account_id = str(response.account_id)
        logger.info("sandbox_account_opened", account_id=account_id)
        return account_id

    async def close_sandbox_account(self, account_id: str) -> None:
        """Закрывает указанный счёт в песочнице."""
        from t_tech.invest.grpc.schemas import CloseSandboxAccountRequest

        request = CloseSandboxAccountRequest(account_id=account_id)
        await retry_async(
            lambda: self._channel.services.sandbox.close_sandbox_account(request=request),
            idempotency_key=f"close-{account_id}",
            operation_name="close_sandbox_account",
        )
        logger.info("sandbox_account_closed", account_id=account_id)

    async def sandbox_pay_in(
        self, account_id: str, amount: Decimal, currency: str = "rub"
    ) -> Decimal:
        """Пополняет баланс виртуального счёта в песочнице."""
        from t_tech.invest.grpc.schemas import MoneyValue, SandboxPayInRequest

        units = int(amount)
        nano = int((amount - Decimal(units)) * NANO)
        request = SandboxPayInRequest(
            account_id=account_id,
            amount=MoneyValue(currency=currency, units=units, nano=nano),
        )
        response = await retry_async(
            lambda: self._channel.services.sandbox.sandbox_pay_in(request=request),
            idempotency_key=f"payin-{account_id}-{int(datetime.now(tz=UTC).timestamp())}",
            operation_name="sandbox_pay_in",
        )
        bal = money_value_to_decimal(getattr(response, "balance", None))
        logger.info("sandbox_pay_in_completed", account_id=account_id, balance=str(bal))
        return bal

    async def get_portfolio(self) -> PortfolioState | None:
        """Портфель счёта в песочнице."""
        from t_tech.invest.grpc.schemas import PortfolioRequest

        if not self._account_id:
            return None
        self._assert_managed_account(self._account_id)
        request = PortfolioRequest(account_id=self._account_id)
        try:
            response = await retry_read_safe(
                lambda: self._channel.services.sandbox.get_sandbox_portfolio(request=request)
            )
            return portfolio_response_to_domain(response, account_id=self._account_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sandbox_portfolio_lookup_failed", account_id=self._account_id, error=str(exc)
            )
            return None

    async def get_open_positions(self) -> list[Position]:
        """Открытые позиции в песочнице."""
        from t_tech.invest.grpc.schemas import PositionsRequest

        if not self._account_id:
            return []
        self._assert_managed_account(self._account_id)
        request = PositionsRequest(account_id=self._account_id)
        try:
            response = await retry_read_safe(
                lambda: self._channel.services.sandbox.get_sandbox_positions(request=request)
            )
            return await self._positions_from_response(response)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sandbox_positions_lookup_failed", account_id=self._account_id, error=str(exc)
            )
            return []


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
