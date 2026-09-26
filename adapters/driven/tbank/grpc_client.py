"""gRPC-подключение к T-Invest API.

Неймспейс SDK — ``t_tech.invest.grpc`` (старый ``tinkoff.invest`` удалён,
использовать его нельзя). Клиенты — контекстные менеджеры, поэтому канал
создаётся асинхронной фабрикой и живёт всё время работы процесса.

Импорт SDK — ленивый: если пакет не установлен, старт падает с понятной
ошибкой, а не с ``ModuleNotFoundError`` в случайном месте.

TLS: SDK самостоятельно создает gRPC credentials и не принимает
``channel_credentials``. Поддерживаемый путь — ``SSL_TBANK_VERIFY=True``:
SDK использует встроенный корневой сертификат НУЦ Минцифры РФ.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import structlog

from adapters.driven.tbank.tls import configure_sdk_tls
from config.settings import Settings, target_for_mode

logger = structlog.get_logger(__name__)


class SdkUnavailableError(RuntimeError):
    """SDK T-Invest не установлен."""


def _import_async_client() -> Any:
    try:
        from t_tech.invest.grpc import AsyncClient  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - зависит от установки
        msg = (
            "Пакет t-tech-investments не установлен. Установите его из реестра Т-Банка:\n"
            "  pip install t-tech-investments "
            "--index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
        )
        raise SdkUnavailableError(msg) from exc
    return AsyncClient


class TInvestChannel:
    """Долгоживущее подключение к API."""

    __slots__ = ("_client", "_exit_stack", "_services", "_target", "_token")

    def __init__(self, target: str, token: str) -> None:
        self._target = target
        self._token = token
        self._client: Any = None
        self._exit_stack: AsyncExitStack | None = None
        self._services: Any = None

    @classmethod
    async def create(cls, settings: Settings) -> TInvestChannel:
        """Асинхронная фабрика: входит в контекст клиента SDK."""
        async_client = _import_async_client()
        target = target_for_mode(settings)
        configure_sdk_tls()

        channel = cls(
            target=target,
            token=settings.tbank.api_token.get_secret_value(),
        )

        client = async_client(
            token=channel._token,
            target=target,
            app_name="red-bot",
        )
        await channel._enter_client(client)
        logger.info("tinvest_channel_created", target=target)
        return channel

    async def _enter_client(self, client: Any) -> None:
        """Enter and retain an SDK async context manager for channel lifetime."""
        stack = AsyncExitStack()
        try:
            services = await stack.enter_async_context(client)
        except BaseException:
            await stack.aclose()
            raise
        self._client = client
        self._exit_stack = stack
        self._services = services

    @property
    def services(self) -> Any:
        """Объект ``services`` SDK: ``services.market_data``, ``services.orders``, ..."""
        if self._services is None:
            msg = "Канал не открыт: сначала вызовите TInvestChannel.create"
            raise RuntimeError(msg)
        return self._services

    @property
    def target(self) -> str:
        return self._target

    async def aclose(self) -> None:
        if self._exit_stack is not None:
            stack, self._exit_stack = self._exit_stack, None
            await stack.aclose()
            self._client = None
            self._services = None
            logger.info("tinvest_channel_closed", target=self._target)


async def create_channel(settings: Settings) -> TInvestChannel:
    """Публичная точка создания канала (используется в ``composition``)."""
    return await TInvestChannel.create(settings)
