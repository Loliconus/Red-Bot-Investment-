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

from typing import TYPE_CHECKING, Any

import structlog

from adapters.driven.tbank.broker_adapter import TBankBrokerAdapter
from adapters.driven.tbank.grpc_client import TInvestChannel
from adapters.driven.tbank.market_data_adapter import TBankMarketDataAdapter
from adapters.driven.tbank.tls import create_ssl_channel_credentials, resolve_ca_path
from config.settings import Settings

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
    """Исполнение в песочнице. Блокировка чужого счёта работает так же."""

    async def open_sandbox_account(self) -> str:
        """Создаёт счёт в песочнице (разово, при первичной настройке)."""
        from t_tech.invest.grpc.schemas import OpenSandboxAccountRequest

        response = await self._channel.services.sandbox.open_sandbox_account(
            request=OpenSandboxAccountRequest()
        )
        account_id = str(response.account_id)
        logger.info("sandbox_account_opened", account_id=account_id)
        return account_id


async def create_sandbox_adapters(
    settings: Settings,
) -> tuple[SandboxMarketDataAdapter, SandboxBrokerAdapter]:
    """Создаёт пару адаптеров песочницы."""
    channel = await SandboxChannel.create(settings)
    market_data = SandboxMarketDataAdapter(channel)
    broker = SandboxBrokerAdapter(channel, account_id=settings.tbank.account_id)
    return market_data, broker


__all__ = [
    "SandboxBrokerAdapter",
    "SandboxChannel",
    "SandboxMarketDataAdapter",
    "create_sandbox_adapters",
]
